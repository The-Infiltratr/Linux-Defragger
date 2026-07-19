#include <ctype.h>
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <linux/fs.h>
#include <pthread.h>
#include <signal.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/stat.h>
#include <sys/sysmacros.h>
#include <sys/types.h>
#include <unistd.h>

#define PROGRAM_NAME "fat32defrag"
#define PROGRAM_VERSION "1.5.0"
#define FAT32_MASK UINT32_C(0x0FFFFFFF)
#define FAT32_EOC_MIN UINT32_C(0x0FFFFFF8)
#define FAT32_BAD UINT32_C(0x0FFFFFF7)

typedef enum {
    FAT_TYPE_12 = 12,
    FAT_TYPE_16 = 16,
    FAT_TYPE_32 = 32
} FatType;
#define MAX_RECURSION_DEPTH 128
#define JOURNAL_MAGIC "FAT32DEFRAG-JOURNAL-1"
#define COMPACT_JOURNAL_MAGIC "FAT32DEFRAG-COMPACT-JOURNAL-1"

typedef struct {
    size_t ram_limit;
    size_t workers;
    bool rotational;
    uint64_t bytes_read;
    uint64_t bytes_written;
    uint64_t read_extents;
    uint64_t write_extents;
} IoConfig;

static IoConfig g_io;
static volatile sig_atomic_t g_stop_requested = 0;

typedef struct {
    uint32_t start;
    uint32_t end;
    uint64_t free_count;
    uint64_t used_count;
    uint64_t fragmented_count;
    uint64_t directory_count;
    uint64_t bad_count;
} LiveMapCell;

static size_t g_live_map_cells = 0;
static LiveMapCell *g_live_map_previous = NULL;
static size_t g_live_map_previous_count = 0;

static void request_stop(int signo) {
    (void)signo;
    g_stop_requested = 1;
}

static uint16_t rd16(const uint8_t *p) {
    return (uint16_t)((uint16_t)p[0] | ((uint16_t)p[1] << 8));
}

static uint32_t rd32(const uint8_t *p) {
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) |
           ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

static void wr16(uint8_t *p, uint16_t v) {
    p[0] = (uint8_t)(v & UINT16_C(0x00FF));
    p[1] = (uint8_t)((v >> 8) & UINT16_C(0x00FF));
}

static void wr32(uint8_t *p, uint32_t v) {
    p[0] = (uint8_t)(v & UINT32_C(0x000000FF));
    p[1] = (uint8_t)((v >> 8) & UINT32_C(0x000000FF));
    p[2] = (uint8_t)((v >> 16) & UINT32_C(0x000000FF));
    p[3] = (uint8_t)((v >> 24) & UINT32_C(0x000000FF));
}

static void die_errno(const char *what) {
    fprintf(stderr, "%s: %s: %s\n", PROGRAM_NAME, what, strerror(errno));
    exit(EXIT_FAILURE);
}

static void die_msg(const char *msg) {
    fprintf(stderr, "%s: %s\n", PROGRAM_NAME, msg);
    exit(EXIT_FAILURE);
}

static void *xmalloc(size_t n) {
    void *p = malloc(n == 0 ? 1 : n);
    if (p == NULL) die_errno("malloc");
    return p;
}

static void *xcalloc(size_t n, size_t s) {
    void *p = calloc(n == 0 ? 1 : n, s == 0 ? 1 : s);
    if (p == NULL) die_errno("calloc");
    return p;
}

static void *xrealloc(void *ptr, size_t n) {
    void *p = realloc(ptr, n == 0 ? 1 : n);
    if (p == NULL) die_errno("realloc");
    return p;
}

static char *xstrdup(const char *s) {
    char *p = strdup(s);
    if (p == NULL) die_errno("strdup");
    return p;
}

typedef struct {
    int fd;
    char *path;
    bool writable;
    bool is_block;
    uint64_t size_bytes;
} Device;

static uint64_t available_memory_bytes(void) {
    FILE *fp = fopen("/proc/meminfo", "r");
    if (fp != NULL) {
        char key[64];
        unsigned long long value = 0;
        char unit[16];
        while (fscanf(fp, "%63s %llu %15s", key, &value, unit) == 3) {
            if (strcmp(key, "MemAvailable:") == 0) {
                fclose(fp);
                return (uint64_t)value * UINT64_C(1024);
            }
        }
        fclose(fp);
    }
    long pages = sysconf(_SC_AVPHYS_PAGES);
    long page_size = sysconf(_SC_PAGESIZE);
    if (pages > 0 && page_size > 0) {
        return (uint64_t)(unsigned long)pages * (uint64_t)(unsigned long)page_size;
    }
    return UINT64_C(512) * 1024 * 1024;
}

static size_t automatic_ram_limit(void) {
    uint64_t available = available_memory_bytes();
    uint64_t chosen = available / 4;
    const uint64_t minimum = UINT64_C(64) * 1024 * 1024;
    const uint64_t maximum = UINT64_C(8) * 1024 * 1024 * 1024;
    if (chosen < minimum) chosen = minimum;
    if (chosen > maximum) chosen = maximum;
    if (chosen > SIZE_MAX) chosen = SIZE_MAX;
    return (size_t)chosen;
}

static size_t online_cpu_count(void) {
    long cpus = sysconf(_SC_NPROCESSORS_ONLN);
    return cpus > 0 ? (size_t)cpus : 1;
}

static bool device_is_rotational(const Device *dev) {
    if (!dev->is_block) return false;
    struct stat st;
    if (fstat(dev->fd, &st) != 0) return false;
    char path[128];
    snprintf(path, sizeof(path), "/sys/dev/block/%u:%u/queue/rotational",
             major(st.st_rdev), minor(st.st_rdev));
    FILE *fp = fopen(path, "r");
    if (fp == NULL) return false;
    int value = 0;
    bool ok = fscanf(fp, "%d", &value) == 1;
    fclose(fp);
    return ok && value != 0;
}

static size_t automatic_worker_count(bool rotational) {
    size_t cpus = online_cpu_count();
    if (rotational) return 1;
    if (cpus > 8) cpus = 8;
    return cpus == 0 ? 1 : cpus;
}

typedef struct {
    uint32_t *v;
    size_t len;
    size_t cap;
} U32Vec;

static void u32vec_push(U32Vec *a, uint32_t v) {
    if (a->len == a->cap) {
        size_t new_cap = a->cap == 0 ? 16 : a->cap * 2;
        a->v = xrealloc(a->v, new_cap * sizeof(*a->v));
        a->cap = new_cap;
    }
    a->v[a->len++] = v;
}

static void u32vec_free(U32Vec *a) {
    free(a->v);
    memset(a, 0, sizeof(*a));
}

typedef struct {
    char *path;
    uint64_t dirent_offset;
    uint32_t first_cluster;
    uint32_t size_bytes;
    uint8_t attr;
    bool is_dir;
    U32Vec chain;
    size_t fragments;
} FileRecord;

typedef struct {
    FileRecord *v;
    size_t len;
    size_t cap;
} FileList;

typedef struct {
    uint64_t offset;
    uint32_t target_cluster;
} DirRef;

typedef struct {
    DirRef *v;
    size_t len;
    size_t cap;
} DirRefList;

static void dirreflist_push(DirRefList *list, DirRef ref) {
    if (list->len == list->cap) {
        size_t new_cap = list->cap == 0 ? 128 : list->cap * 2;
        list->v = xrealloc(list->v, new_cap * sizeof(*list->v));
        list->cap = new_cap;
    }
    list->v[list->len++] = ref;
}

static void dirreflist_free(DirRefList *list) {
    free(list->v);
    memset(list, 0, sizeof(*list));
}

static void filelist_push(FileList *list, FileRecord rec) {
    if (list->len == list->cap) {
        size_t new_cap = list->cap == 0 ? 128 : list->cap * 2;
        list->v = xrealloc(list->v, new_cap * sizeof(*list->v));
        list->cap = new_cap;
    }
    list->v[list->len++] = rec;
}

static void filelist_free(FileList *list) {
    for (size_t i = 0; i < list->len; i++) {
        free(list->v[i].path);
        u32vec_free(&list->v[i].chain);
    }
    free(list->v);
    memset(list, 0, sizeof(*list));
}

typedef struct {
    Device dev;
    FatType fat_type;
    uint16_t bytes_per_sector;
    uint8_t sectors_per_cluster;
    uint16_t reserved_sectors;
    uint8_t fat_count;
    uint16_t ext_flags;
    bool fat_mirroring;
    uint8_t active_fat;
    uint32_t sectors_per_fat;
    uint32_t total_sectors;
    uint32_t root_cluster;
    uint16_t root_entry_count;
    uint64_t root_dir_offset;
    uint64_t root_dir_size;
    bool root_is_fixed;
    uint16_t fsinfo_sector;
    uint16_t backup_boot_sector;
    uint32_t volume_id;
    uint64_t fat0_offset;
    uint64_t data_offset;
    uint64_t cluster_size;
    uint32_t cluster_count;
    uint32_t max_cluster;
    uint32_t fat_entry_count;
    uint32_t *fat;
    uint8_t *visited_dirs;
    uint8_t *claimed_clusters;
    uint32_t *chain_seen;
    uint32_t chain_generation;
    bool recovery_mode;
} Fat32;

static void emit_live_map_update(Fat32 *fs);

typedef enum {
    J_PREPARED = 0,
    J_DATA_COPIED = 1,
    J_DEST_LINKED = 2,
    J_SWITCHED = 3,
    J_OLD_FREED = 4
} JournalStage;

typedef struct {
    char *device_path;
    uint32_t volume_id;
    JournalStage stage;
    uint64_t dirent_offset;
    uint32_t old_first;
    uint32_t dest_start;
    U32Vec source;
} Journal;

typedef struct {
    uint32_t source;
    uint32_t destination;
    uint32_t next;
    uint32_t predecessor;
} CompactMove;

typedef struct {
    uint64_t offset;
    uint32_t old_target;
    uint32_t new_target;
} CompactDirPatch;

typedef struct {
    char *device_path;
    uint32_t volume_id;
    JournalStage stage;
    uint32_t root_old;
    uint32_t root_new;
    CompactMove *moves;
    size_t move_count;
    CompactDirPatch *dir_patches;
    size_t dir_patch_count;
} CompactJournal;

static ssize_t full_pread(int fd, void *buf, size_t count, uint64_t offset) {
    uint8_t *p = buf;
    size_t done = 0;
    while (done < count) {
        ssize_t n = pread(fd, p + done, count - done, (off_t)(offset + done));
        if (n < 0) {
            if (errno == EINTR) continue;
            return -1;
        }
        if (n == 0) break;
        done += (size_t)n;
    }
    return (ssize_t)done;
}

static ssize_t full_pwrite(int fd, const void *buf, size_t count, uint64_t offset) {
    const uint8_t *p = buf;
    size_t done = 0;
    while (done < count) {
        ssize_t n = pwrite(fd, p + done, count - done, (off_t)(offset + done));
        if (n < 0) {
            if (errno == EINTR) continue;
            return -1;
        }
        if (n == 0) {
            errno = EIO;
            return -1;
        }
        done += (size_t)n;
    }
    return (ssize_t)done;
}

static bool block_device_is_mounted(dev_t rdev) {
    FILE *fp = fopen("/proc/self/mountinfo", "r");
    if (fp == NULL) die_errno("open /proc/self/mountinfo");
    unsigned target_major = major(rdev);
    unsigned target_minor = minor(rdev);
    char *line = NULL;
    size_t cap = 0;
    bool mounted = false;
    while (getline(&line, &cap, fp) >= 0) {
        unsigned maj = 0, min = 0;
        char *p = line;
        int field = 0;
        while (*p != '\0') {
            while (*p == ' ') p++;
            if (*p == '\0') break;
            field++;
            char *end = strchr(p, ' ');
            if (field == 3 && sscanf(p, "%u:%u", &maj, &min) == 2) {
                if (maj == target_major && min == target_minor) {
                    mounted = true;
                    break;
                }
            }
            if (end == NULL) break;
            p = end + 1;
        }
        if (mounted) break;
    }
    free(line);
    fclose(fp);
    return mounted;
}

static Device device_open(const char *path, bool writable) {
    struct stat st;
    if (stat(path, &st) != 0) die_errno("stat device");
    bool is_block = S_ISBLK(st.st_mode);
    if (!is_block && !S_ISREG(st.st_mode)) {
        die_msg("target must be a block device or a regular filesystem image");
    }
    if (is_block && block_device_is_mounted(st.st_rdev)) {
        die_msg("refusing to open a mounted block device; unmount it first");
    }

    int flags = writable ? O_RDWR : O_RDONLY;
    flags |= O_CLOEXEC;
    if (is_block) flags |= O_EXCL;
    int fd = open(path, flags);
    if (fd < 0) die_errno("open target");

    uint64_t size = 0;
    if (is_block) {
        if (ioctl(fd, BLKGETSIZE64, &size) != 0) die_errno("BLKGETSIZE64");
    } else {
        size = (uint64_t)st.st_size;
    }

    Device d = {
        .fd = fd,
        .path = xstrdup(path),
        .writable = writable,
        .is_block = is_block,
        .size_bytes = size,
    };
    return d;
}

static void device_close(Device *d) {
    if (d->fd >= 0 && close(d->fd) != 0) {
        fprintf(stderr, "%s: warning: close: %s\n", PROGRAM_NAME, strerror(errno));
    }
    free(d->path);
    memset(d, 0, sizeof(*d));
    d->fd = -1;
}

static uint64_t cluster_offset(const Fat32 *fs, uint32_t cluster) {
    if (cluster < 2 || cluster > fs->max_cluster) die_msg("cluster number outside data region");
    return fs->data_offset + (uint64_t)(cluster - 2) * fs->cluster_size;
}

typedef struct {
    uint64_t disk_offset;
    size_t buffer_offset;
    size_t length;
} IoExtent;

typedef struct {
    IoExtent *v;
    size_t len;
    size_t cap;
} IoExtentList;

static void ioextent_push(IoExtentList *list, IoExtent extent) {
    if (list->len == list->cap) {
        size_t new_cap = list->cap == 0 ? 16 : list->cap * 2;
        list->v = xrealloc(list->v, new_cap * sizeof(*list->v));
        list->cap = new_cap;
    }
    list->v[list->len++] = extent;
}

static void ioextent_free(IoExtentList *list) {
    free(list->v);
    memset(list, 0, sizeof(*list));
}

static int compare_extent_disk_offset(const void *a, const void *b) {
    const IoExtent *ea = a;
    const IoExtent *eb = b;
    if (ea->disk_offset < eb->disk_offset) return -1;
    if (ea->disk_offset > eb->disk_offset) return 1;
    return 0;
}

static IoExtentList build_cluster_extents(const Fat32 *fs, const uint32_t *clusters,
                                          size_t count) {
    IoExtentList list = {0};
    if (count == 0) return list;
    size_t first = 0;
    for (size_t i = 1; i <= count; i++) {
        bool end = i == count || clusters[i] != clusters[i - 1] + 1;
        if (!end) continue;
        size_t cluster_count = i - first;
        if (cluster_count > SIZE_MAX / (size_t)fs->cluster_size) {
            ioextent_free(&list);
            die_msg("I/O extent is too large for this build");
        }
        ioextent_push(&list, (IoExtent){
            .disk_offset = cluster_offset(fs, clusters[first]),
            .buffer_offset = first * (size_t)fs->cluster_size,
            .length = cluster_count * (size_t)fs->cluster_size,
        });
        first = i;
    }
    return list;
}

typedef struct {
    int fd;
    uint8_t *buffer;
    const IoExtent *extents;
    size_t extent_count;
    size_t next_extent;
    pthread_mutex_t lock;
    int error_number;
} ReadQueue;

static void *read_extent_worker(void *arg) {
    ReadQueue *queue = arg;
    for (;;) {
        pthread_mutex_lock(&queue->lock);
        size_t index = queue->next_extent++;
        pthread_mutex_unlock(&queue->lock);
        if (index >= queue->extent_count) break;
        const IoExtent *extent = &queue->extents[index];
        if (full_pread(queue->fd, queue->buffer + extent->buffer_offset,
                       extent->length, extent->disk_offset) != (ssize_t)extent->length) {
            int saved = errno == 0 ? EIO : errno;
            pthread_mutex_lock(&queue->lock);
            if (queue->error_number == 0) queue->error_number = saved;
            pthread_mutex_unlock(&queue->lock);
            break;
        }
    }
    return NULL;
}

static void read_extents(Fat32 *fs, uint8_t *buffer, IoExtentList *extents) {
    if (extents->len == 0) return;
    qsort(extents->v, extents->len, sizeof(extents->v[0]), compare_extent_disk_offset);
    size_t workers = g_io.workers;
    if (workers > extents->len) workers = extents->len;
    if (workers < 1) workers = 1;

    ReadQueue queue = {
        .fd = fs->dev.fd,
        .buffer = buffer,
        .extents = extents->v,
        .extent_count = extents->len,
    };
    if (pthread_mutex_init(&queue.lock, NULL) != 0) die_msg("cannot initialise I/O worker lock");

    if (workers == 1) {
        (void)read_extent_worker(&queue);
    } else {
        pthread_t *threads = xmalloc(workers * sizeof(*threads));
        size_t started = 0;
        for (; started < workers; started++) {
            int rc = pthread_create(&threads[started], NULL, read_extent_worker, &queue);
            if (rc != 0) {
                queue.error_number = rc;
                break;
            }
        }
        for (size_t i = 0; i < started; i++) (void)pthread_join(threads[i], NULL);
        free(threads);
    }
    pthread_mutex_destroy(&queue.lock);
    if (queue.error_number != 0) {
        errno = queue.error_number;
        die_errno("read source extent");
    }
    for (size_t i = 0; i < extents->len; i++) {
        g_io.bytes_read += extents->v[i].length;
    }
    g_io.read_extents += extents->len;
}

static void write_extents(Fat32 *fs, const uint8_t *buffer, IoExtentList *extents) {
    if (extents->len == 0) return;
    qsort(extents->v, extents->len, sizeof(extents->v[0]), compare_extent_disk_offset);
    for (size_t i = 0; i < extents->len; i++) {
        const IoExtent *extent = &extents->v[i];
        if (full_pwrite(fs->dev.fd, buffer + extent->buffer_offset,
                        extent->length, extent->disk_offset) != (ssize_t)extent->length) {
            die_errno("write destination extent");
        }
        g_io.bytes_written += extent->length;
    }
    g_io.write_extents += extents->len;
}

static void copy_cluster_mapping_buffered(Fat32 *fs, const uint32_t *sources,
                                          const uint32_t *destinations, size_t count) {
    if (count == 0) return;
    size_t cluster_size = (size_t)fs->cluster_size;
    size_t clusters_per_chunk = g_io.ram_limit / cluster_size;
    if (clusters_per_chunk == 0) clusters_per_chunk = 1;
    if (clusters_per_chunk > count) clusters_per_chunk = count;
    if (clusters_per_chunk > SIZE_MAX / cluster_size) die_msg("RAM buffer size overflow");
    size_t allocation = clusters_per_chunk * cluster_size;
    void *raw = NULL;
    int rc = posix_memalign(&raw, 4096, allocation);
    if (rc != 0) {
        errno = rc;
        die_errno("allocate aligned RAM buffer");
    }
    uint8_t *buffer = raw;

    for (size_t base = 0; base < count; base += clusters_per_chunk) {
        size_t chunk = count - base;
        if (chunk > clusters_per_chunk) chunk = clusters_per_chunk;
        IoExtentList source_extents = build_cluster_extents(fs, sources + base, chunk);
        IoExtentList destination_extents = build_cluster_extents(fs, destinations + base, chunk);
        read_extents(fs, buffer, &source_extents);
        write_extents(fs, buffer, &destination_extents);
        ioextent_free(&source_extents);
        ioextent_free(&destination_extents);
    }
    free(buffer);
}

static uint32_t fat_mask(const Fat32 *fs) {
    return fs->fat_type == FAT_TYPE_12 ? UINT32_C(0x0FFF) :
           fs->fat_type == FAT_TYPE_16 ? UINT32_C(0xFFFF) : FAT32_MASK;
}

static uint32_t fat_eoc_min(const Fat32 *fs) {
    return fs->fat_type == FAT_TYPE_12 ? UINT32_C(0x0FF8) :
           fs->fat_type == FAT_TYPE_16 ? UINT32_C(0xFFF8) : FAT32_EOC_MIN;
}

static uint32_t fat_bad_value(const Fat32 *fs) {
    return fs->fat_type == FAT_TYPE_12 ? UINT32_C(0x0FF7) :
           fs->fat_type == FAT_TYPE_16 ? UINT32_C(0xFFF7) : FAT32_BAD;
}

static uint32_t fat_reserved_min(const Fat32 *fs) {
    return fs->fat_type == FAT_TYPE_12 ? UINT32_C(0x0FF0) :
           fs->fat_type == FAT_TYPE_16 ? UINT32_C(0xFFF0) : UINT32_C(0x0FFFFFF0);
}

static uint32_t fat_eoc_value(const Fat32 *fs) { return fat_mask(fs); }

static const char *fat_type_name(const Fat32 *fs) {
    return fs->fat_type == FAT_TYPE_12 ? "FAT12" :
           fs->fat_type == FAT_TYPE_16 ? "FAT16" : "FAT32";
}

static size_t fat_entry_byte_offset(const Fat32 *fs, uint32_t cluster) {
    if (fs->fat_type == FAT_TYPE_12) return (size_t)cluster + (size_t)cluster / 2;
    if (fs->fat_type == FAT_TYPE_16) return (size_t)cluster * 2;
    return (size_t)cluster * 4;
}

static uint32_t decode_fat_entry(const Fat32 *fs, const uint8_t *raw, size_t fat_bytes,
                                 uint32_t cluster) {
    size_t off = fat_entry_byte_offset(fs, cluster);
    if (fs->fat_type == FAT_TYPE_12) {
        if (off + 1 >= fat_bytes) die_msg("FAT12 entry outside loaded table");
        uint16_t pair = rd16(raw + off);
        return (cluster & 1u) ? (uint32_t)(pair >> 4) : (uint32_t)(pair & 0x0FFFu);
    }
    if (fs->fat_type == FAT_TYPE_16) {
        if (off + 1 >= fat_bytes) die_msg("FAT16 entry outside loaded table");
        return rd16(raw + off);
    }
    if (off + 3 >= fat_bytes) die_msg("FAT32 entry outside loaded table");
    return rd32(raw + off) & FAT32_MASK;
}

static uint32_t fat_value(const Fat32 *fs, uint32_t cluster) {
    if (cluster >= fs->fat_entry_count) die_msg("FAT entry outside loaded table");
    return fs->fat[cluster] & fat_mask(fs);
}

static bool fat_is_eoc_for(const Fat32 *fs, uint32_t v) {
    return (v & fat_mask(fs)) >= fat_eoc_min(fs);
}

static bool fat_is_free(const Fat32 *fs, uint32_t cluster) {
    return fat_value(fs, cluster) == 0;
}

static void fat32_load(Fat32 *fs, Device dev, bool allow_mirror_mismatch) {
    memset(fs, 0, sizeof(*fs));
    fs->dev = dev;
    fs->recovery_mode = allow_mirror_mismatch;

    uint8_t boot[512];
    if (full_pread(fs->dev.fd, boot, sizeof(boot), 0) != (ssize_t)sizeof(boot)) {
        die_errno("read boot sector");
    }
    if (boot[510] != 0x55 || boot[511] != 0xAA) die_msg("invalid boot-sector signature");

    fs->bytes_per_sector = rd16(boot + 11);
    fs->sectors_per_cluster = boot[13];
    fs->reserved_sectors = rd16(boot + 14);
    fs->fat_count = boot[16];
    fs->root_entry_count = rd16(boot + 17);
    uint16_t total16 = rd16(boot + 19);
    uint16_t fat16_sectors = rd16(boot + 22);
    uint32_t total32 = rd32(boot + 32);
    uint32_t fat32_sectors = rd32(boot + 36);
    fs->total_sectors = total16 != 0 ? total16 : total32;

    if (!(fs->bytes_per_sector == 512 || fs->bytes_per_sector == 1024 ||
          fs->bytes_per_sector == 2048 || fs->bytes_per_sector == 4096)) {
        die_msg("unsupported bytes-per-sector value");
    }
    if (fs->sectors_per_cluster == 0 ||
        (fs->sectors_per_cluster & (fs->sectors_per_cluster - 1)) != 0) {
        die_msg("invalid sectors-per-cluster value");
    }
    if (fs->reserved_sectors == 0 || fs->fat_count == 0 || fs->total_sectors == 0) {
        die_msg("invalid FAT layout fields");
    }

    uint32_t root_dir_sectors =
        ((uint32_t)fs->root_entry_count * 32u + fs->bytes_per_sector - 1u) /
        fs->bytes_per_sector;
    fs->sectors_per_fat = fat16_sectors != 0 ? fat16_sectors : fat32_sectors;
    if (fs->sectors_per_fat == 0) die_msg("invalid FAT size");
    uint64_t fat_sectors_total = (uint64_t)fs->fat_count * fs->sectors_per_fat;
    if ((uint64_t)fs->reserved_sectors + fat_sectors_total + root_dir_sectors >=
        fs->total_sectors) die_msg("invalid FAT/data layout");
    uint32_t data_sectors = fs->total_sectors - fs->reserved_sectors -
                            (uint32_t)fat_sectors_total - root_dir_sectors;
    fs->cluster_count = data_sectors / fs->sectors_per_cluster;
    fs->fat_type = fs->cluster_count < 4085 ? FAT_TYPE_12 :
                   fs->cluster_count < 65525 ? FAT_TYPE_16 : FAT_TYPE_32;
    fs->max_cluster = fs->cluster_count + 1;
    fs->cluster_size = (uint64_t)fs->bytes_per_sector * fs->sectors_per_cluster;
    fs->fat0_offset = (uint64_t)fs->reserved_sectors * fs->bytes_per_sector;
    fs->root_dir_offset = ((uint64_t)fs->reserved_sectors + fat_sectors_total) *
                          fs->bytes_per_sector;
    fs->root_dir_size = (uint64_t)root_dir_sectors * fs->bytes_per_sector;
    fs->root_is_fixed = fs->fat_type != FAT_TYPE_32;
    fs->data_offset = fs->root_dir_offset + fs->root_dir_size;

    if (fs->fat_type == FAT_TYPE_32) {
        fs->ext_flags = rd16(boot + 40);
        uint16_t fs_version = rd16(boot + 42);
        if (fs_version != 0) die_msg("unsupported nonzero FAT32 filesystem version");
        fs->fat_mirroring = (fs->ext_flags & UINT16_C(0x0080)) == 0;
        fs->active_fat = fs->fat_mirroring ? 0 : (uint8_t)(fs->ext_flags & UINT16_C(0x000F));
        if (fs->active_fat >= fs->fat_count) die_msg("active FAT index is outside the FAT count");
        fs->root_cluster = rd32(boot + 44) & FAT32_MASK;
        fs->fsinfo_sector = rd16(boot + 48);
        fs->backup_boot_sector = rd16(boot + 50);
        fs->volume_id = rd32(boot + 67);
        if (fs->root_entry_count != 0 || fat16_sectors != 0)
            die_msg("FAT32 layout fields are inconsistent");
        if (fs->root_cluster < 2 || fs->root_cluster > fs->max_cluster)
            die_msg("invalid root cluster");
    } else {
        fs->fat_mirroring = true;
        fs->active_fat = 0;
        fs->root_cluster = 0;
        fs->fsinfo_sector = 0;
        fs->backup_boot_sector = 0;
        fs->volume_id = rd32(boot + 39);
        if (fs->root_entry_count == 0 || fat16_sectors == 0)
            die_msg("FAT12/FAT16 layout fields are inconsistent");
    }

    uint64_t volume_bytes = (uint64_t)fs->total_sectors * fs->bytes_per_sector;
    if (volume_bytes > fs->dev.size_bytes) die_msg("filesystem extends beyond target size");

    size_t fat_bytes = (size_t)fs->sectors_per_fat * fs->bytes_per_sector;
    if (fs->fat_type == FAT_TYPE_12) fs->fat_entry_count = (uint32_t)((fat_bytes * 2u) / 3u);
    else if (fs->fat_type == FAT_TYPE_16) fs->fat_entry_count = (uint32_t)(fat_bytes / 2u);
    else fs->fat_entry_count = (uint32_t)(fat_bytes / 4u);
    if (fs->fat_entry_count <= fs->max_cluster) die_msg("FAT is too small for the data region");

    uint8_t *raw = xmalloc(fat_bytes);
    uint64_t active_fat_offset = fs->fat0_offset + (uint64_t)fs->active_fat * fat_bytes;
    if (full_pread(fs->dev.fd, raw, fat_bytes, active_fat_offset) != (ssize_t)fat_bytes)
        die_errno("read active FAT");
    fs->fat = xcalloc((size_t)fs->fat_entry_count, sizeof(*fs->fat));
    for (uint32_t i = 0; i < fs->fat_entry_count; i++)
        fs->fat[i] = decode_fat_entry(fs, raw, fat_bytes, i);

    if (fs->fat_type == FAT_TYPE_32) {
        if ((fs->fat[1] & UINT32_C(0x08000000)) == 0)
            die_msg("FAT32 clean-shutdown bit is clear; repair or cleanly unmount the volume first");
        if ((fs->fat[1] & UINT32_C(0x04000000)) == 0)
            die_msg("FAT32 hard-error bit is clear; repair the volume before defragmenting");
    } else if (fs->fat_type == FAT_TYPE_16) {
        if ((fs->fat[1] & UINT32_C(0x8000)) == 0)
            die_msg("FAT16 clean-shutdown bit is clear; repair or cleanly unmount the volume first");
        if ((fs->fat[1] & UINT32_C(0x4000)) == 0)
            die_msg("FAT16 hard-error bit is clear; repair the volume before defragmenting");
    }

    if (fs->fat_mirroring) {
        uint8_t *other = xmalloc(fat_bytes);
        for (uint8_t copy = 0; copy < fs->fat_count; copy++) {
            if (copy == fs->active_fat) continue;
            uint64_t off = fs->fat0_offset + (uint64_t)copy * fat_bytes;
            if (full_pread(fs->dev.fd, other, fat_bytes, off) != (ssize_t)fat_bytes)
                die_errno("read mirrored FAT");
            for (uint32_t i = 0; i <= fs->max_cluster; i++) {
                if (decode_fat_entry(fs, other, fat_bytes, i) != fat_value(fs, i)) {
                    if (!allow_mirror_mismatch)
                        die_msg("mirrored FAT copies disagree; repair the filesystem before defragmenting");
                    fprintf(stderr, "%s: warning: mirrored FAT copies disagree; recovery will rewrite journalled entries\n", PROGRAM_NAME);
                    copy = fs->fat_count;
                    break;
                }
            }
        }
        free(other);
    }
    free(raw);
    fs->visited_dirs = xcalloc((size_t)fs->max_cluster + 1, 1);
    fs->claimed_clusters = xcalloc((size_t)fs->max_cluster + 1, 1);
    fs->chain_seen = xcalloc((size_t)fs->max_cluster + 1, sizeof(*fs->chain_seen));
    fs->chain_generation = 0;
}

static void fat32_unload(Fat32 *fs) {
    free(fs->fat);
    free(fs->visited_dirs);
    free(fs->claimed_clusters);
    free(fs->chain_seen);
    Device d = fs->dev;
    memset(fs, 0, sizeof(*fs));
    device_close(&d);
}

static void fat32_sync(Fat32 *fs) {
    if (fsync(fs->dev.fd) != 0) die_errno("fsync target");
}

static void fat32_write_entry(Fat32 *fs, uint32_t cluster, uint32_t new_value) {
    if (!fs->dev.writable) die_msg("internal error: attempted write on read-only target");
    if (cluster >= fs->fat_entry_count) die_msg("attempted FAT write outside table");
    size_t fat_bytes = (size_t)fs->sectors_per_fat * fs->bytes_per_sector;
    size_t byte_off = fat_entry_byte_offset(fs, cluster);
    size_t span = fs->fat_type == FAT_TYPE_32 ? 4u : 2u;
    uint8_t first_copy = fs->fat_mirroring ? 0 : fs->active_fat;
    uint8_t end_copy = fs->fat_mirroring ? fs->fat_count : (uint8_t)(fs->active_fat + 1);
    for (uint8_t copy = first_copy; copy < end_copy; copy++) {
        uint8_t raw[4] = {0};
        uint64_t off = fs->fat0_offset + (uint64_t)copy * fat_bytes + byte_off;
        if (full_pread(fs->dev.fd, raw, span, off) != (ssize_t)span)
            die_errno("read FAT entry before update");
        if (fs->fat_type == FAT_TYPE_12) {
            uint16_t pair = rd16(raw);
            uint32_t value = new_value & UINT32_C(0x0FFF);
            pair = (cluster & 1u) ? (uint16_t)((pair & 0x000Fu) | (value << 4))
                                  : (uint16_t)((pair & 0xF000u) | value);
            wr16(raw, pair);
        } else if (fs->fat_type == FAT_TYPE_16) {
            wr16(raw, (uint16_t)new_value);
        } else {
            uint32_t old = rd32(raw);
            wr32(raw, (old & UINT32_C(0xF0000000)) | (new_value & FAT32_MASK));
        }
        if (full_pwrite(fs->dev.fd, raw, span, off) != (ssize_t)span)
            die_errno("write FAT entry");
    }
    fs->fat[cluster] = new_value & fat_mask(fs);
}

typedef struct {
    uint32_t cluster;
    uint32_t value;
} FatUpdate;

static int compare_u32_values(const void *a, const void *b) {
    uint32_t av = *(const uint32_t *)a;
    uint32_t bv = *(const uint32_t *)b;
    return av < bv ? -1 : av > bv ? 1 : 0;
}

static void fat32_apply_updates(Fat32 *fs, const FatUpdate *updates, size_t count) {
    if (count == 0) return;
    if (!fs->dev.writable) die_msg("internal error: attempted bulk FAT write on read-only target");
    if (fs->fat_type != FAT_TYPE_32 || (fs->recovery_mode && fs->fat_mirroring)) {
        for (size_t i = 0; i < count; i++) fat32_write_entry(fs, updates[i].cluster, updates[i].value);
        return;
    }
    uint32_t *sectors = xmalloc(count * sizeof(*sectors));
    for (size_t i = 0; i < count; i++) {
        uint32_t cluster = updates[i].cluster;
        if (cluster >= fs->fat_entry_count) { free(sectors); die_msg("attempted bulk FAT write outside table"); }
        fs->fat[cluster] = (fs->fat[cluster] & UINT32_C(0xF0000000)) | (updates[i].value & FAT32_MASK);
        sectors[i] = (uint32_t)(((uint64_t)cluster * 4) / fs->bytes_per_sector);
    }
    qsort(sectors, count, sizeof(*sectors), compare_u32_values);
    size_t unique = 0;
    for (size_t i = 0; i < count; i++) if (unique == 0 || sectors[i] != sectors[unique - 1]) sectors[unique++] = sectors[i];
    size_t fat_bytes = (size_t)fs->sectors_per_fat * fs->bytes_per_sector;
    uint8_t first_copy = fs->fat_mirroring ? 0 : fs->active_fat;
    uint8_t end_copy = fs->fat_mirroring ? fs->fat_count : (uint8_t)(fs->active_fat + 1);
    size_t run_start_index = 0;
    while (run_start_index < unique) {
        size_t run_end_index = run_start_index + 1;
        while (run_end_index < unique && sectors[run_end_index] == sectors[run_end_index - 1] + 1) run_end_index++;
        uint32_t first_sector = sectors[run_start_index];
        size_t sector_count = run_end_index - run_start_index;
        size_t bytes = sector_count * fs->bytes_per_sector;
        uint8_t *raw = xmalloc(bytes);
        size_t first_entry = ((size_t)first_sector * fs->bytes_per_sector) / 4;
        size_t entry_count = bytes / 4;
        for (size_t e = 0; e < entry_count; e++) wr32(raw + e * 4, fs->fat[first_entry + e]);
        for (uint8_t copy = first_copy; copy < end_copy; copy++) {
            uint64_t off = fs->fat0_offset + (uint64_t)copy * fat_bytes + (uint64_t)first_sector * fs->bytes_per_sector;
            if (full_pwrite(fs->dev.fd, raw, bytes, off) != (ssize_t)bytes) { free(raw); free(sectors); die_errno("write FAT sector batch"); }
        }
        free(raw);
        run_start_index = run_end_index;
    }
    free(sectors);
}

static U32Vec fat32_read_chain(Fat32 *fs, uint32_t first) {
    U32Vec chain = {0};
    if (first == 0) return chain;
    if (first < 2 || first > fs->max_cluster) die_msg("file begins at invalid cluster");
    fs->chain_generation++;
    if (fs->chain_generation == 0) {
        memset(fs->chain_seen, 0, ((size_t)fs->max_cluster + 1) * sizeof(*fs->chain_seen));
        fs->chain_generation = 1;
    }
    uint32_t generation = fs->chain_generation;
    uint32_t cur = first;
    for (;;) {
        if (cur < 2 || cur > fs->max_cluster) die_msg("cluster chain points outside data region");
        if (fs->chain_seen[cur] == generation) die_msg("cluster-chain loop detected");
        fs->chain_seen[cur] = generation;
        u32vec_push(&chain, cur);
        uint32_t next = fat_value(fs, cur);
        if (fat_is_eoc_for(fs, next)) break;
        if (next == 0) die_msg("allocated chain terminates in a free FAT entry");
        if (next == fat_bad_value(fs) || (next >= fat_reserved_min(fs) && next < fat_eoc_min(fs))) {
            die_msg("cluster chain reaches a bad or reserved FAT entry");
        }
        cur = next;
        if (chain.len > fs->cluster_count) die_msg("cluster chain exceeds volume cluster count");
    }
    return chain;
}

static size_t chain_fragments(const U32Vec *chain) {
    if (chain->len == 0) return 0;
    size_t fragments = 1;
    for (size_t i = 1; i < chain->len; i++) {
        if (chain->v[i] != chain->v[i - 1] + 1) fragments++;
    }
    return fragments;
}

static void claim_chain(Fat32 *fs, const U32Vec *chain, const char *owner) {
    for (size_t i = 0; i < chain->len; i++) {
        uint32_t c = chain->v[i];
        if (fs->claimed_clusters[c]) {
            fprintf(stderr, "%s: cross-linked cluster %" PRIu32 " while scanning %s\n",
                    PROGRAM_NAME, c, owner);
            exit(EXIT_FAILURE);
        }
        fs->claimed_clusters[c] = 1;
    }
}

static void short_name(const uint8_t entry[32], char out[14]) {
    char base[9] = {0};
    char ext[4] = {0};
    memcpy(base, entry, 8);
    memcpy(ext, entry + 8, 3);
    for (int i = 7; i >= 0 && base[i] == ' '; i--) base[i] = '\0';
    for (int i = 2; i >= 0 && ext[i] == ' '; i--) ext[i] = '\0';
    if ((uint8_t)base[0] == 0x05) base[0] = (char)0xE5;
    if (ext[0] != '\0') snprintf(out, 14, "%s.%s", base, ext);
    else snprintf(out, 14, "%s", base);
}

static char *path_join(const char *parent, const char *name) {
    size_t lp = strlen(parent);
    size_t ln = strlen(name);
    bool slash = lp > 0 && parent[lp - 1] != '/';
    char *s = xmalloc(lp + (slash ? 1 : 0) + ln + 1);
    memcpy(s, parent, lp);
    size_t pos = lp;
    if (slash) s[pos++] = '/';
    memcpy(s + pos, name, ln + 1);
    return s;
}

static bool is_dot_entry(const uint8_t e[32]) {
    if (e[0] != '.') return false;
    if (e[1] == ' ' || e[1] == '.') return true;
    return false;
}

static void scan_directory(Fat32 *fs, uint32_t first_cluster, const char *path,
                           FileList *files, DirRefList *dir_refs, unsigned depth) {
    if (depth > MAX_RECURSION_DEPTH) die_msg("directory recursion limit exceeded");
    bool fixed_root = fs->root_is_fixed && first_cluster == 0 && depth == 0;
    U32Vec chain = {0};
    uint64_t unit_size = fs->cluster_size;
    size_t units = 0;
    if (fixed_root) {
        unit_size = fs->root_dir_size;
        units = fs->root_dir_size == 0 ? 0 : 1;
    } else {
        if (first_cluster < 2 || first_cluster > fs->max_cluster) die_msg("invalid directory cluster");
        if (fs->visited_dirs[first_cluster]) die_msg("directory cycle or cross-link detected");
        fs->visited_dirs[first_cluster] = 1;
        chain = fat32_read_chain(fs, first_cluster);
        units = chain.len;
    }

    uint8_t *buf = xmalloc((size_t)unit_size);
    bool end_directory = false;
    for (size_t ci = 0; ci < units && !end_directory; ci++) {
        uint64_t c_off = fixed_root ? fs->root_dir_offset : cluster_offset(fs, chain.v[ci]);
        if (full_pread(fs->dev.fd, buf, (size_t)unit_size, c_off) != (ssize_t)unit_size) {
            die_errno("read directory data");
        }
        for (uint64_t pos = 0; pos + 32 <= unit_size; pos += 32) {
            uint8_t *e = buf + pos;
            if (e[0] == 0x00) {
                end_directory = true;
                break;
            }
            if (e[0] == 0xE5) continue;
            uint8_t attr = e[11];
            if (attr == 0x0F) continue;
            if ((attr & 0x08) != 0) continue;
            uint32_t first = rd16(e + 26);
            if (fs->fat_type == FAT_TYPE_32) first |= (uint32_t)rd16(e + 20) << 16;
            first &= fat_mask(fs);
            if (dir_refs != NULL && first != 0) {
                dirreflist_push(dir_refs, (DirRef){
                    .offset = c_off + pos,
                    .target_cluster = first,
                });
            }
            if (is_dot_entry(e)) continue;

            char name[14];
            short_name(e, name);
            if (name[0] == '\0') continue;
            uint32_t size = rd32(e + 28);
            bool is_dir = (attr & 0x10) != 0;
            char *full = path_join(path, name);
            FileRecord rec = {
                .path = full,
                .dirent_offset = c_off + pos,
                .first_cluster = first,
                .size_bytes = size,
                .attr = attr,
                .is_dir = is_dir,
            };
            if (first != 0) {
                rec.chain = fat32_read_chain(fs, first);
                rec.fragments = chain_fragments(&rec.chain);
                claim_chain(fs, &rec.chain, full);
            }
            if (!is_dir) {
                size_t expected = size == 0 ? 0 : (size_t)(((uint64_t)size + fs->cluster_size - 1) / fs->cluster_size);
                if ((size == 0 && first != 0) || (size != 0 && first == 0) || rec.chain.len != expected) {
                    fprintf(stderr, "%s: file size and cluster-chain length disagree for %s\n",
                            PROGRAM_NAME, full);
                    exit(EXIT_FAILURE);
                }
            }
            filelist_push(files, rec);
            if (is_dir && first != 0) {
                scan_directory(fs, first, full, files, dir_refs, depth + 1);
            }
        }
    }
    free(buf);
    u32vec_free(&chain);
}

static U32Vec filesystem_root_chain(Fat32 *fs);

static FileList scan_files(Fat32 *fs, DirRefList *dir_refs) {
    memset(fs->visited_dirs, 0, (size_t)fs->max_cluster + 1);
    memset(fs->claimed_clusters, 0, (size_t)fs->max_cluster + 1);
    if (dir_refs != NULL) {
        dirreflist_free(dir_refs);
    }
    if (!fs->root_is_fixed) {
        U32Vec root_chain = filesystem_root_chain(fs);
        claim_chain(fs, &root_chain, "<root directory>");
        u32vec_free(&root_chain);
    }
    FileList list = {0};
    scan_directory(fs, fs->root_is_fixed ? 0 : fs->root_cluster, "", &list, dir_refs, 0);

    for (uint32_t c = 2; c <= fs->max_cluster; c++) {
        uint32_t v = fat_value(fs, c);
        if (v == 0 || v == fat_bad_value(fs)) continue;
        if (v >= fat_reserved_min(fs) && v < fat_eoc_min(fs)) {
            fprintf(stderr, "%s: reserved FAT value at cluster %" PRIu32 "\n", PROGRAM_NAME, c);
            exit(EXIT_FAILURE);
        }
        if (!fs->claimed_clusters[c]) {
            fprintf(stderr, "%s: allocated but unreferenced cluster %" PRIu32
                            " detected; repair lost chains before defragmenting\n", PROGRAM_NAME, c);
            exit(EXIT_FAILURE);
        }
    }
    return list;
}

static uint64_t count_free_clusters(const Fat32 *fs) {
    uint64_t count = 0;
    for (uint32_t c = 2; c <= fs->max_cluster; c++) if (fat_is_free(fs, c)) count++;
    return count;
}

static bool find_free_run(const Fat32 *fs, size_t needed, uint32_t *start_out) {
    if (needed == 0 || needed > fs->cluster_count) return false;
    size_t run = 0;
    uint32_t start = 0;
    for (uint32_t c = 2; c <= fs->max_cluster; c++) {
        if (fat_is_free(fs, c)) {
            if (run == 0) start = c;
            run++;
            if (run >= needed) {
                *start_out = start;
                return true;
            }
        } else {
            run = 0;
        }
    }
    return false;
}

static bool find_free_run_reserved(const Fat32 *fs, const uint8_t *reserved,
                                   size_t needed, uint32_t *start_out) {
    if (needed == 0 || needed > fs->cluster_count) return false;
    size_t run = 0;
    uint32_t start = 0;
    for (uint32_t c = 2; c <= fs->max_cluster; c++) {
        if (fat_is_free(fs, c) && !reserved[c]) {
            if (run == 0) start = c;
            run++;
            if (run >= needed) {
                *start_out = start;
                return true;
            }
        } else {
            run = 0;
        }
    }
    return false;
}

static void write_dirent_first_cluster(Fat32 *fs, uint64_t offset, uint32_t first) {
    uint8_t entry[32];
    if (full_pread(fs->dev.fd, entry, sizeof(entry), offset) != (ssize_t)sizeof(entry)) {
        die_errno("read directory entry");
    }
    if (fs->fat_type == FAT_TYPE_32) wr16(entry + 20, (uint16_t)((first >> 16) & UINT32_C(0xFFFF)));
    else wr16(entry + 20, 0);
    wr16(entry + 26, (uint16_t)(first & UINT32_C(0xFFFF)));
    if (full_pwrite(fs->dev.fd, entry, sizeof(entry), offset) != (ssize_t)sizeof(entry)) {
        die_errno("write directory entry");
    }
}

typedef struct {
    uint64_t sector_offset;
    uint64_t entry_offset;
    uint32_t new_target;
} DirentSectorPatch;

static int compare_dirent_sector_patch(const void *a, const void *b) {
    const DirentSectorPatch *pa = a;
    const DirentSectorPatch *pb = b;
    if (pa->sector_offset < pb->sector_offset) return -1;
    if (pa->sector_offset > pb->sector_offset) return 1;
    if (pa->entry_offset < pb->entry_offset) return -1;
    if (pa->entry_offset > pb->entry_offset) return 1;
    if (pa->new_target < pb->new_target) return -1;
    if (pa->new_target > pb->new_target) return 1;
    return 0;
}

static size_t write_dirent_first_clusters_batched(Fat32 *fs,
                                                    const CompactDirPatch *patches,
                                                    size_t count) {
    if (count == 0) return 0;
    size_t bps = fs->bytes_per_sector;
    if (bps < 32 || bps % 32 != 0) {
        die_msg("FAT sector size cannot contain aligned directory entries");
    }
    DirentSectorPatch *ordered = xmalloc(count * sizeof(*ordered));
    for (size_t i = 0; i < count; i++) {
        uint64_t offset = patches[i].offset;
        uint64_t sector_offset = offset - (offset % bps);
        uint64_t within = offset - sector_offset;
        if (within % 32 != 0 || within + 32 > bps) {
            free(ordered);
            die_msg("directory entry is not aligned inside a FAT sector");
        }
        ordered[i] = (DirentSectorPatch){
            .sector_offset = sector_offset,
            .entry_offset = offset,
            .new_target = patches[i].new_target,
        };
    }
    qsort(ordered, count, sizeof(*ordered), compare_dirent_sector_patch);

    uint8_t *sector = xmalloc(bps);
    size_t sector_writes = 0;
    size_t i = 0;
    while (i < count) {
        uint64_t sector_offset = ordered[i].sector_offset;
        if (full_pread(fs->dev.fd, sector, bps, sector_offset) != (ssize_t)bps) {
            free(sector);
            free(ordered);
            die_errno("read directory sector batch");
        }
        size_t j = i;
        while (j < count && ordered[j].sector_offset == sector_offset) {
            size_t within = (size_t)(ordered[j].entry_offset - sector_offset);
            if (j > i && ordered[j].entry_offset == ordered[j - 1].entry_offset &&
                ordered[j].new_target != ordered[j - 1].new_target) {
                free(sector);
                free(ordered);
                die_msg("conflicting directory-entry patches in one transaction");
            }
            wr16(sector + within + 20, fs->fat_type == FAT_TYPE_32 ?
                 (uint16_t)((ordered[j].new_target >> 16) & UINT32_C(0xFFFF)) : 0);
            wr16(sector + within + 26,
                 (uint16_t)(ordered[j].new_target & UINT32_C(0xFFFF)));
            j++;
        }
        if (full_pwrite(fs->dev.fd, sector, bps, sector_offset) != (ssize_t)bps) {
            free(sector);
            free(ordered);
            die_errno("write directory sector batch");
        }
        sector_writes++;
        i = j;
    }
    free(sector);
    free(ordered);
    return sector_writes;
}

static uint32_t read_dirent_first_cluster(Fat32 *fs, uint64_t offset) {
    uint8_t entry[32];
    if (full_pread(fs->dev.fd, entry, sizeof(entry), offset) != (ssize_t)sizeof(entry)) {
        die_errno("read directory entry");
    }
    uint32_t first = rd16(entry + 26);
    if (fs->fat_type == FAT_TYPE_32) first |= (uint32_t)rd16(entry + 20) << 16;
    return first & fat_mask(fs);
}

static uint32_t data_offset_to_cluster(const Fat32 *fs, uint64_t offset) {
    if (offset < fs->data_offset) die_msg("directory reference lies outside the data region");
    uint64_t relative = offset - fs->data_offset;
    uint64_t index = relative / fs->cluster_size;
    if (index >= fs->cluster_count) die_msg("directory reference lies beyond the data region");
    return (uint32_t)index + 2;
}

static uint64_t move_offset_between_clusters(const Fat32 *fs, uint64_t offset,
                                             uint32_t source, uint32_t destination) {
    uint64_t source_offset = cluster_offset(fs, source);
    if (offset < source_offset || offset >= source_offset + fs->cluster_size) {
        die_msg("internal error: reference offset is not inside its source cluster");
    }
    return cluster_offset(fs, destination) + (offset - source_offset);
}

static void write_boot_root_cluster_one(Fat32 *fs, uint16_t sector, uint32_t root_cluster) {
    if (sector == UINT16_C(0xFFFF) || sector >= fs->reserved_sectors) return;
    size_t bps = fs->bytes_per_sector;
    uint8_t *buf = xmalloc(bps);
    uint64_t off = (uint64_t)sector * bps;
    if (full_pread(fs->dev.fd, buf, bps, off) != (ssize_t)bps) die_errno("read boot sector");
    if (bps < 512 || buf[510] != 0x55 || buf[511] != 0xAA) {
        free(buf);
        die_msg("invalid FAT32 boot-sector copy while updating root cluster");
    }
    wr32(buf + 44, root_cluster & FAT32_MASK);
    if (full_pwrite(fs->dev.fd, buf, bps, off) != (ssize_t)bps) die_errno("write boot sector");
    free(buf);
}

static void write_root_cluster(Fat32 *fs, uint32_t root_cluster) {
    if (fs->root_is_fixed) die_msg("internal error: FAT12/FAT16 root directory cannot be relocated");
    write_boot_root_cluster_one(fs, 0, root_cluster);
    if (fs->backup_boot_sector != 0 && fs->backup_boot_sector != UINT16_C(0xFFFF)) {
        write_boot_root_cluster_one(fs, fs->backup_boot_sector, root_cluster);
    }
    fs->root_cluster = root_cluster;
}

static void update_fsinfo_next_free_one(Fat32 *fs, uint16_t sector, uint32_t next_free) {
    if (sector == 0 || sector == UINT16_C(0xFFFF) || sector >= fs->reserved_sectors) return;
    size_t bps = fs->bytes_per_sector;
    uint8_t *buf = xmalloc(bps);
    uint64_t off = (uint64_t)sector * bps;
    if (full_pread(fs->dev.fd, buf, bps, off) != (ssize_t)bps) die_errno("read FSInfo");
    if (bps >= 512 && rd32(buf) == UINT32_C(0x41615252) &&
        rd32(buf + 484) == UINT32_C(0x61417272) &&
        rd32(buf + 508) == UINT32_C(0xAA550000)) {
        wr32(buf + 492, next_free);
        if (full_pwrite(fs->dev.fd, buf, bps, off) != (ssize_t)bps) die_errno("write FSInfo");
    }
    free(buf);
}

static void update_fsinfo_next_free(Fat32 *fs, uint32_t next_free) {
    if (fs->fat_type != FAT_TYPE_32) return;
    update_fsinfo_next_free_one(fs, fs->fsinfo_sector, next_free);
    if (fs->backup_boot_sector != 0 && fs->backup_boot_sector != UINT16_C(0xFFFF)) {
        uint32_t backup_fsinfo = (uint32_t)fs->backup_boot_sector + fs->fsinfo_sector;
        if (backup_fsinfo < fs->reserved_sectors) {
            update_fsinfo_next_free_one(fs, (uint16_t)backup_fsinfo, next_free);
        }
    }
}

static char *default_journal_path(const char *device_path) {
    const char *base = strrchr(device_path, '/');
    base = base == NULL ? device_path : base + 1;
    size_t n = strlen(base) + 40;
    char *path = xmalloc(n);
    snprintf(path, n, ".fat32defrag-%s.journal", base);
    return path;
}

static void journal_free(Journal *j) {
    free(j->device_path);
    u32vec_free(&j->source);
    memset(j, 0, sizeof(*j));
}

static void compact_journal_free(CompactJournal *j) {
    free(j->device_path);
    free(j->moves);
    free(j->dir_patches);
    memset(j, 0, sizeof(*j));
}

static void compact_journal_add_move(CompactJournal *j, CompactMove move) {
    j->moves = xrealloc(j->moves, (j->move_count + 1) * sizeof(*j->moves));
    j->moves[j->move_count++] = move;
}

static void compact_journal_add_dir_patch(CompactJournal *j, CompactDirPatch patch) {
    j->dir_patches = xrealloc(j->dir_patches,
                              (j->dir_patch_count + 1) * sizeof(*j->dir_patches));
    j->dir_patches[j->dir_patch_count++] = patch;
}

static void fsync_parent_directory(const char *path) {
    char *copy = xstrdup(path);
    char *slash = strrchr(copy, '/');
    const char *dir = ".";
    if (slash != NULL) {
        if (slash == copy) slash[1] = '\0';
        else *slash = '\0';
        dir = copy;
    }
    int fd = open(dir, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
    if (fd >= 0) {
        (void)fsync(fd);
        close(fd);
    }
    free(copy);
}

static void journal_write(const char *path, const Journal *j) {
    size_t tmpn = strlen(path) + 5;
    char *tmp = xmalloc(tmpn);
    snprintf(tmp, tmpn, "%s.tmp", path);
    FILE *fp = fopen(tmp, "w");
    if (fp == NULL) die_errno("create journal");
    fprintf(fp, "%s\n", JOURNAL_MAGIC);
    fprintf(fp, "device=%s\n", j->device_path);
    fprintf(fp, "volume_id=%08" PRIx32 "\n", j->volume_id);
    fprintf(fp, "stage=%d\n", (int)j->stage);
    fprintf(fp, "dirent_offset=%" PRIu64 "\n", j->dirent_offset);
    fprintf(fp, "old_first=%" PRIu32 "\n", j->old_first);
    fprintf(fp, "dest_start=%" PRIu32 "\n", j->dest_start);
    fprintf(fp, "count=%zu\n", j->source.len);
    fputs("source=", fp);
    for (size_t i = 0; i < j->source.len; i++) {
        if (i != 0) fputc(',', fp);
        fprintf(fp, "%" PRIu32, j->source.v[i]);
    }
    fputc('\n', fp);
    if (fflush(fp) != 0) die_errno("flush journal");
    if (fsync(fileno(fp)) != 0) die_errno("fsync journal");
    if (fclose(fp) != 0) die_errno("close journal");
    if (rename(tmp, path) != 0) die_errno("install journal");
    fsync_parent_directory(path);
    free(tmp);
}

static void compact_journal_write(const char *path, const CompactJournal *j) {
    size_t tmpn = strlen(path) + 5;
    char *tmp = xmalloc(tmpn);
    snprintf(tmp, tmpn, "%s.tmp", path);
    FILE *fp = fopen(tmp, "w");
    if (fp == NULL) die_errno("create compact journal");
    fprintf(fp, "%s\n", COMPACT_JOURNAL_MAGIC);
    fprintf(fp, "device=%s\n", j->device_path);
    fprintf(fp, "volume_id=%08" PRIx32 "\n", j->volume_id);
    fprintf(fp, "stage=%d\n", (int)j->stage);
    fprintf(fp, "root_old=%" PRIu32 "\n", j->root_old);
    fprintf(fp, "root_new=%" PRIu32 "\n", j->root_new);
    fprintf(fp, "move_count=%zu\n", j->move_count);
    for (size_t i = 0; i < j->move_count; i++) {
        const CompactMove *m = &j->moves[i];
        fprintf(fp, "move=%" PRIu32 ",%" PRIu32 ",%" PRIu32 ",%" PRIu32 "\n",
                m->source, m->destination, m->next, m->predecessor);
    }
    fprintf(fp, "dir_patch_count=%zu\n", j->dir_patch_count);
    for (size_t i = 0; i < j->dir_patch_count; i++) {
        const CompactDirPatch *p = &j->dir_patches[i];
        fprintf(fp, "dir_patch=%" PRIu64 ",%" PRIu32 ",%" PRIu32 "\n",
                p->offset, p->old_target, p->new_target);
    }
    if (fflush(fp) != 0) die_errno("flush compact journal");
    if (fsync(fileno(fp)) != 0) die_errno("fsync compact journal");
    if (fclose(fp) != 0) die_errno("close compact journal");
    if (rename(tmp, path) != 0) die_errno("install compact journal");
    fsync_parent_directory(path);
    free(tmp);
}

static bool journal_has_magic(const char *path, const char *magic) {
    FILE *fp = fopen(path, "r");
    if (fp == NULL) die_errno("open journal");
    char *line = NULL;
    size_t cap = 0;
    bool match = getline(&line, &cap, fp) >= 0;
    if (match) line[strcspn(line, "\r\n")] = '\0';
    match = match && strcmp(line, magic) == 0;
    free(line);
    fclose(fp);
    return match;
}

static Journal journal_read(const char *path) {
    FILE *fp = fopen(path, "r");
    if (fp == NULL) die_errno("open journal");
    Journal j = {0};
    char *line = NULL;
    size_t cap = 0;
    if (getline(&line, &cap, fp) < 0 || strcmp(line, JOURNAL_MAGIC "\n") != 0) {
        die_msg("invalid journal header");
    }
    size_t expected_count = 0;
    while (getline(&line, &cap, fp) >= 0) {
        line[strcspn(line, "\r\n")] = '\0';
        char *eq = strchr(line, '=');
        if (eq == NULL) continue;
        *eq++ = '\0';
        if (strcmp(line, "device") == 0) j.device_path = xstrdup(eq);
        else if (strcmp(line, "volume_id") == 0) j.volume_id = (uint32_t)strtoul(eq, NULL, 16);
        else if (strcmp(line, "stage") == 0) j.stage = (JournalStage)strtol(eq, NULL, 10);
        else if (strcmp(line, "dirent_offset") == 0) j.dirent_offset = strtoull(eq, NULL, 10);
        else if (strcmp(line, "old_first") == 0) j.old_first = (uint32_t)strtoul(eq, NULL, 10);
        else if (strcmp(line, "dest_start") == 0) j.dest_start = (uint32_t)strtoul(eq, NULL, 10);
        else if (strcmp(line, "count") == 0) expected_count = (size_t)strtoull(eq, NULL, 10);
        else if (strcmp(line, "source") == 0) {
            char *save = NULL;
            for (char *tok = strtok_r(eq, ",", &save); tok != NULL; tok = strtok_r(NULL, ",", &save)) {
                u32vec_push(&j.source, (uint32_t)strtoul(tok, NULL, 10));
            }
        }
    }
    free(line);
    fclose(fp);
    if (j.device_path == NULL || j.source.len != expected_count || j.source.len == 0) {
        journal_free(&j);
        die_msg("journal is incomplete or corrupt");
    }
    return j;
}

static CompactJournal compact_journal_read(const char *path) {
    FILE *fp = fopen(path, "r");
    if (fp == NULL) die_errno("open compact journal");
    CompactJournal j = {0};
    char *line = NULL;
    size_t cap = 0;
    if (getline(&line, &cap, fp) < 0 || strcmp(line, COMPACT_JOURNAL_MAGIC "\n") != 0) {
        die_msg("invalid compact journal header");
    }
    size_t expected_moves = 0;
    size_t expected_patches = 0;
    while (getline(&line, &cap, fp) >= 0) {
        line[strcspn(line, "\r\n")] = '\0';
        char *eq = strchr(line, '=');
        if (eq == NULL) continue;
        *eq++ = '\0';
        if (strcmp(line, "device") == 0) j.device_path = xstrdup(eq);
        else if (strcmp(line, "volume_id") == 0) j.volume_id = (uint32_t)strtoul(eq, NULL, 16);
        else if (strcmp(line, "stage") == 0) j.stage = (JournalStage)strtol(eq, NULL, 10);
        else if (strcmp(line, "root_old") == 0) j.root_old = (uint32_t)strtoul(eq, NULL, 10);
        else if (strcmp(line, "root_new") == 0) j.root_new = (uint32_t)strtoul(eq, NULL, 10);
        else if (strcmp(line, "move_count") == 0) expected_moves = (size_t)strtoull(eq, NULL, 10);
        else if (strcmp(line, "dir_patch_count") == 0) {
            expected_patches = (size_t)strtoull(eq, NULL, 10);
        } else if (strcmp(line, "move") == 0) {
            CompactMove m = {0};
            if (sscanf(eq, "%" SCNu32 ",%" SCNu32 ",%" SCNu32 ",%" SCNu32,
                       &m.source, &m.destination, &m.next, &m.predecessor) != 4) {
                compact_journal_free(&j);
                die_msg("invalid compact journal move record");
            }
            compact_journal_add_move(&j, m);
        } else if (strcmp(line, "dir_patch") == 0) {
            CompactDirPatch p = {0};
            if (sscanf(eq, "%" SCNu64 ",%" SCNu32 ",%" SCNu32,
                       &p.offset, &p.old_target, &p.new_target) != 3) {
                compact_journal_free(&j);
                die_msg("invalid compact journal directory patch");
            }
            compact_journal_add_dir_patch(&j, p);
        }
    }
    free(line);
    fclose(fp);
    if (j.device_path == NULL || j.move_count == 0 || j.move_count != expected_moves ||
        j.dir_patch_count != expected_patches || j.root_old < 2 || j.root_new < 2) {
        compact_journal_free(&j);
        die_msg("compact journal is incomplete or corrupt");
    }
    return j;
}

static void journal_remove(const char *path) {
    if (unlink(path) != 0 && errno != ENOENT) die_errno("remove journal");
    fsync_parent_directory(path);
}

static bool path_exists(const char *path) {
    return access(path, F_OK) == 0;
}

static void free_cluster_list(Fat32 *fs, const uint32_t *clusters, size_t count) {
    FatUpdate *updates = xmalloc(count * sizeof(*updates));
    for (size_t i = 0; i < count; i++) {
        updates[i] = (FatUpdate){.cluster = clusters[i], .value = 0};
    }
    fat32_apply_updates(fs, updates, count);
    free(updates);
}

static void free_contiguous_run(Fat32 *fs, uint32_t start, size_t count) {
    FatUpdate *updates = xmalloc(count * sizeof(*updates));
    for (size_t i = 0; i < count; i++) {
        updates[i] = (FatUpdate){.cluster = start + (uint32_t)i, .value = 0};
    }
    fat32_apply_updates(fs, updates, count);
    free(updates);
}

static void recover_journal(Fat32 *fs, const char *journal_path) {
    Journal j = journal_read(journal_path);
    if (strcmp(j.device_path, fs->dev.path) != 0) die_msg("journal belongs to a different device path");
    if (j.volume_id != fs->volume_id) die_msg("journal volume ID does not match target");
    if (j.dest_start < 2 || j.source.len > fs->cluster_count ||
        j.dest_start > fs->max_cluster - (uint32_t)(j.source.len - 1)) {
        die_msg("journal destination range is invalid");
    }

    uint32_t current = read_dirent_first_cluster(fs, j.dirent_offset);
    fprintf(stderr, "Recovering interrupted move at journal stage %d...\n", (int)j.stage);
    if (current == j.dest_start) {
        for (size_t i = 0; i < j.source.len; i++) {
            uint32_t dest = j.dest_start + (uint32_t)i;
            uint32_t next = (i + 1 == j.source.len) ? fat_eoc_value(fs) : dest + 1;
            fat32_write_entry(fs, dest, next);
        }
        free_cluster_list(fs, j.source.v, j.source.len);
        update_fsinfo_next_free(fs, j.source.v[0]);
        fat32_sync(fs);
        fprintf(stderr, "Recovery completed by keeping the new contiguous chain.\n");
    } else if (current == j.old_first) {
        free_contiguous_run(fs, j.dest_start, j.source.len);
        fat32_sync(fs);
        fprintf(stderr, "Recovery completed by rolling back the destination chain.\n");
    } else {
        journal_free(&j);
        die_msg("directory entry points to neither old nor new chain; manual recovery required");
    }
    journal_remove(journal_path);
    journal_free(&j);
}

static bool fat_value_is_cluster(const Fat32 *fs, uint32_t value) {
    value &= fat_mask(fs);
    return value >= 2 && value <= fs->max_cluster;
}

static uint32_t *compact_build_map(const Fat32 *fs, const CompactJournal *j) {
    uint32_t *map = xcalloc((size_t)fs->max_cluster + 1, sizeof(*map));
    uint8_t *dest_seen = xcalloc((size_t)fs->max_cluster + 1, 1);
    for (size_t i = 0; i < j->move_count; i++) {
        const CompactMove *m = &j->moves[i];
        if (m->source < 2 || m->source > fs->max_cluster ||
            m->destination < 2 || m->destination > fs->max_cluster ||
            m->destination == m->source) {
            free(dest_seen);
            free(map);
            die_msg("compact journal contains an invalid source/destination pair");
        }
        if (map[m->source] != 0 || dest_seen[m->destination]) {
            free(dest_seen);
            free(map);
            die_msg("compact journal contains duplicate source or destination clusters");
        }
        map[m->source] = m->destination;
        dest_seen[m->destination] = 1;
    }
    for (size_t i = 0; i < j->move_count; i++) {
        if (dest_seen[j->moves[i].source]) {
            free(dest_seen);
            free(map);
            die_msg("compact journal source and destination sets overlap");
        }
    }
    free(dest_seen);
    return map;
}

static uint32_t *build_predecessor_table(Fat32 *fs) {
    uint32_t *pred = xcalloc((size_t)fs->max_cluster + 1, sizeof(*pred));
    for (uint32_t c = 2; c <= fs->max_cluster; c++) {
        uint32_t next = fat_value(fs, c);
        if (!fat_value_is_cluster(fs, next)) continue;
        if (pred[next] != 0) {
            free(pred);
            die_msg("multiple FAT predecessors detected while building compaction map");
        }
        pred[next] = c;
    }
    return pred;
}

static CompactJournal make_compact_journal(Fat32 *fs, const DirRefList *dir_refs,
                                            const uint32_t *pred,
                                            const CompactMove *moves, size_t move_count) {
    CompactJournal j = {
        .device_path = xstrdup(fs->dev.path),
        .volume_id = fs->volume_id,
        .stage = J_PREPARED,
        .root_old = fs->root_cluster,
        .root_new = fs->root_cluster,
    };
    for (size_t i = 0; i < move_count; i++) {
        CompactMove m = moves[i];
        m.next = fat_value(fs, m.source);
        m.predecessor = pred[m.source];
        compact_journal_add_move(&j, m);
    }

    uint32_t *map = compact_build_map(fs, &j);
    if (map[j.root_old] != 0) j.root_new = map[j.root_old];

    uint8_t *first_has_reference = xcalloc((size_t)fs->max_cluster + 1, 1);
    for (size_t i = 0; i < dir_refs->len; i++) {
        const DirRef *ref = &dir_refs->v[i];
        uint32_t new_target = map[ref->target_cluster];
        if (new_target == 0) continue;
        uint64_t patch_offset = ref->offset;
        bool in_fixed_root = fs->root_is_fixed && ref->offset >= fs->root_dir_offset &&
                             ref->offset < fs->root_dir_offset + fs->root_dir_size;
        if (!in_fixed_root) {
            uint32_t container = data_offset_to_cluster(fs, ref->offset);
            if (map[container] != 0) {
                patch_offset = move_offset_between_clusters(fs, ref->offset, container, map[container]);
            }
        }
        compact_journal_add_dir_patch(&j, (CompactDirPatch){
            .offset = patch_offset,
            .old_target = ref->target_cluster,
            .new_target = new_target,
        });
        first_has_reference[ref->target_cluster] = 1;
    }

    for (size_t i = 0; i < j.move_count; i++) {
        const CompactMove *m = &j.moves[i];
        if (m->predecessor == 0 && m->source != j.root_old && !first_has_reference[m->source]) {
            free(first_has_reference);
            free(map);
            compact_journal_free(&j);
            die_msg("first cluster has no directory reference during compaction");
        }
    }
    free(first_has_reference);
    free(map);
    return j;
}

static void complete_compact_journal(Fat32 *fs, const char *journal_path,
                                     CompactJournal *j) {
    if (strcmp(j->device_path, fs->dev.path) != 0) {
        die_msg("compact journal belongs to a different device path");
    }
    if (j->volume_id != fs->volume_id) die_msg("compact journal volume ID does not match target");
    uint32_t *map = compact_build_map(fs, j);

    uint32_t *sources = xmalloc(j->move_count * sizeof(*sources));
    uint32_t *destinations = xmalloc(j->move_count * sizeof(*destinations));
    for (size_t i = 0; i < j->move_count; i++) {
        const CompactMove *m = &j->moves[i];
        sources[i] = m->source;
        destinations[i] = m->destination;
    }
    copy_cluster_mapping_buffered(fs, sources, destinations, j->move_count);
    free(sources);
    free(destinations);
    fat32_sync(fs);
    j->stage = J_DATA_COPIED;
    compact_journal_write(journal_path, j);

    FatUpdate *link_updates = xmalloc(j->move_count * 2 * sizeof(*link_updates));
    size_t link_count = 0;
    for (size_t i = 0; i < j->move_count; i++) {
        const CompactMove *m = &j->moves[i];
        uint32_t new_next = m->next;
        if (fat_value_is_cluster(fs, m->next) && map[m->next] != 0) {
            new_next = map[m->next];
        }
        link_updates[link_count++] = (FatUpdate){
            .cluster = m->destination,
            .value = new_next,
        };
    }

    for (size_t i = 0; i < j->move_count; i++) {
        const CompactMove *m = &j->moves[i];
        if (m->predecessor != 0 && map[m->predecessor] == 0) {
            link_updates[link_count++] = (FatUpdate){
                .cluster = m->predecessor,
                .value = m->destination,
            };
        }
    }
    fat32_apply_updates(fs, link_updates, link_count);
    free(link_updates);
    size_t directory_sector_writes =
        write_dirent_first_clusters_batched(fs, j->dir_patches, j->dir_patch_count);
    if (j->dir_patch_count != 0) {
        fprintf(stderr,
                "Directory metadata:      %zu entr%s in %zu sector write%s\n",
                j->dir_patch_count, j->dir_patch_count == 1 ? "y" : "ies",
                directory_sector_writes, directory_sector_writes == 1 ? "" : "s");
    }
    if (j->root_new != j->root_old) write_root_cluster(fs, j->root_new);
    fat32_sync(fs);
    j->stage = J_SWITCHED;
    compact_journal_write(journal_path, j);

    uint32_t next_free = fs->max_cluster;
    uint32_t *old_sources = xmalloc(j->move_count * sizeof(*old_sources));
    for (size_t i = 0; i < j->move_count; i++) {
        old_sources[i] = j->moves[i].source;
        if (j->moves[i].source < next_free) next_free = j->moves[i].source;
    }
    free_cluster_list(fs, old_sources, j->move_count);
    free(old_sources);
    update_fsinfo_next_free(fs, next_free);
    fat32_sync(fs);
    j->stage = J_OLD_FREED;
    compact_journal_write(journal_path, j);

    journal_remove(journal_path);
    free(map);
}

static void recover_compact_journal(Fat32 *fs, const char *journal_path) {
    CompactJournal j = compact_journal_read(journal_path);
    fprintf(stderr, "Recovering interrupted compact batch of %zu cluster%s...\n",
            j.move_count, j.move_count == 1 ? "" : "s");
    complete_compact_journal(fs, journal_path, &j);
    fprintf(stderr, "Compact recovery completed by finishing the recorded batch.\n");
    compact_journal_free(&j);
}


static bool cluster_is_movable_allocation(const Fat32 *fs, uint32_t cluster) {
    uint32_t value = fat_value(fs, cluster);
    return value != 0 && value != fat_bad_value(fs);
}

static uint64_t terminal_free_clusters(const Fat32 *fs) {
    uint64_t free_count = 0;
    for (uint32_t c = fs->max_cluster; c >= 2; c--) {
        if (!fat_is_free(fs, c)) break;
        free_count++;
        if (c == 2) break;
    }
    return free_count;
}

static uint32_t first_free_cluster_hint(const Fat32 *fs) {
    for (uint32_t c = 2; c <= fs->max_cluster; c++) {
        if (fat_is_free(fs, c)) return c;
    }
    return UINT32_C(0xFFFFFFFF);
}

static uint64_t holes_below_high_water(const Fat32 *fs, uint32_t *highest_out) {
    uint32_t highest = 1;
    for (uint32_t c = fs->max_cluster; c >= 2; c--) {
        if (cluster_is_movable_allocation(fs, c)) {
            highest = c;
            break;
        }
        if (c == 2) break;
    }
    uint64_t holes = 0;
    if (highest >= 2) {
        for (uint32_t c = 2; c <= highest; c++) if (fat_is_free(fs, c)) holes++;
    }
    *highest_out = highest;
    return holes;
}

typedef struct {
    size_t clusters_moved;
    size_t transactions;
    size_t whole_objects;
    size_t whole_clusters;
    size_t staged_objects;
    size_t staged_clusters;
    size_t extent_transactions;
    size_t extent_clusters;
    size_t singleton_transactions;
} CompactStats;

typedef struct {
    bool is_dir;
    const U32Vec *chain;
    const char *path;
    uint32_t destination;
} PlannedWholeObject;

static uint32_t chain_min_cluster(const U32Vec *chain) {
    uint32_t minimum = UINT32_MAX;
    for (size_t i = 0; i < chain->len; i++) {
        if (chain->v[i] < minimum) minimum = chain->v[i];
    }
    return minimum;
}

static bool chain_lies_above_destination(const U32Vec *chain, uint32_t destination) {
    if (chain->len == 0 || chain->len > UINT32_MAX) return false;
    uint64_t end64 = (uint64_t)destination + chain->len - 1;
    if (end64 > UINT32_MAX) return false;
    uint32_t end = (uint32_t)end64;
    for (size_t i = 0; i < chain->len; i++) {
        if (chain->v[i] <= end) return false;
    }
    return true;
}

static bool first_free_run_below_high_water(const Fat32 *fs, uint32_t *start_out,
                                             size_t *length_out, uint32_t *highest_out) {
    uint32_t highest = 1;
    for (uint32_t c = fs->max_cluster; c >= 2; c--) {
        if (cluster_is_movable_allocation(fs, c)) {
            highest = c;
            break;
        }
        if (c == 2) break;
    }
    *highest_out = highest;
    if (highest < 2) return false;

    for (uint32_t c = 2; c <= highest; c++) {
        if (!fat_is_free(fs, c)) continue;
        uint32_t start = c;
        size_t length = 0;
        while (c <= highest && fat_is_free(fs, c)) {
            length++;
            if (c == highest) break;
            c++;
        }
        *start_out = start;
        *length_out = length;
        return true;
    }
    return false;
}

static bool better_whole_object(uint32_t minimum, size_t clusters, const char *path,
                                bool have_best, uint32_t best_minimum,
                                size_t best_clusters, const char *best_path) {
    if (!have_best) return true;
    if (minimum != best_minimum) return minimum < best_minimum;
    if (clusters != best_clusters) return clusters > best_clusters;
    return strcmp(path, best_path) < 0;
}

static void compact_execute_moves(Fat32 *fs, const DirRefList *dir_refs,
                                  const char *journal_path,
                                  const CompactMove *moves, size_t move_count) {
    uint32_t *pred = build_predecessor_table(fs);
    CompactJournal journal = make_compact_journal(fs, dir_refs, pred, moves, move_count);
    free(pred);
    compact_journal_write(journal_path, &journal);
    complete_compact_journal(fs, journal_path, &journal);
    compact_journal_free(&journal);
    emit_live_map_update(fs);
}

static CompactStats compact_volume(Fat32 *fs, const char *journal_path,
                                   size_t max_clusters, size_t batch_clusters,
                                   size_t max_transactions) {
    CompactStats stats = {0};
    if (batch_clusters == 0) batch_clusters = 4096;

    for (;;) {
        if (g_stop_requested) {
            fprintf(stderr, "interrupt requested; stopping compaction between transactions\n");
            break;
        }
        if (max_transactions != 0 && stats.transactions >= max_transactions) break;
        size_t remaining = max_clusters == 0 ? SIZE_MAX : max_clusters - stats.clusters_moved;
        if (remaining == 0) break;

        DirRefList dir_refs = {0};
        FileList files = scan_files(fs, &dir_refs);
        U32Vec root_chain = filesystem_root_chain(fs);

        uint32_t hole_start = 0;
        size_t hole_length = 0;
        uint32_t highest = 1;
        if (!first_free_run_below_high_water(fs, &hole_start, &hole_length, &highest)) {
            u32vec_free(&root_chain);
            filelist_free(&files);
            dirreflist_free(&dir_refs);
            break;
        }

        bool *selected_files = xcalloc(files.len, 1);
        bool selected_root = false;
        CompactMove *moves = NULL;
        size_t move_count = 0;
        size_t move_cap = 0;
        PlannedWholeObject *planned = NULL;
        size_t planned_count = 0;
        size_t planned_cap = 0;
        uint32_t destination = hole_start;
        size_t hole_remaining = hole_length;

        for (;;) {
            bool have_best = false;
            bool best_is_root = false;
            size_t best_index = 0;
            const U32Vec *best_chain = NULL;
            const char *best_path = NULL;
            bool best_is_dir = false;
            uint32_t best_minimum = 0;
            size_t available = remaining - move_count;
            if (available == 0 || hole_remaining == 0) break;

            size_t transaction_available = move_count == 0 && batch_clusters < available
                                           ? available : batch_clusters > move_count
                                           ? batch_clusters - move_count : 0;
            if (move_count != 0 && transaction_available == 0) break;

            if (!selected_root && root_chain.len != 0 && root_chain.len <= hole_remaining &&
                root_chain.len <= available &&
                (move_count == 0 || root_chain.len <= transaction_available) &&
                chain_lies_above_destination(&root_chain, destination)) {
                uint32_t minimum = chain_min_cluster(&root_chain);
                if (better_whole_object(minimum, root_chain.len, "<root directory>",
                                        have_best, best_minimum,
                                        best_chain == NULL ? 0 : best_chain->len, best_path)) {
                    have_best = true;
                    best_is_root = true;
                    best_chain = &root_chain;
                    best_path = "<root directory>";
                    best_is_dir = true;
                    best_minimum = minimum;
                }
            }

            for (size_t i = 0; i < files.len; i++) {
                const FileRecord *candidate = &files.v[i];
                if (selected_files[i] || candidate->chain.len == 0 ||
                    candidate->chain.len > hole_remaining || candidate->chain.len > available ||
                    (move_count != 0 && candidate->chain.len > transaction_available) ||
                    !chain_lies_above_destination(&candidate->chain, destination)) {
                    continue;
                }
                uint32_t minimum = chain_min_cluster(&candidate->chain);
                if (better_whole_object(minimum, candidate->chain.len, candidate->path,
                                        have_best, best_minimum,
                                        best_chain == NULL ? 0 : best_chain->len, best_path)) {
                    have_best = true;
                    best_is_root = false;
                    best_index = i;
                    best_chain = &candidate->chain;
                    best_path = candidate->path;
                    best_is_dir = candidate->is_dir;
                    best_minimum = minimum;
                }
            }

            if (!have_best) break;
            if (move_count + best_chain->len > move_cap) {
                size_t new_cap = move_cap == 0 ? best_chain->len : move_cap;
                while (new_cap < move_count + best_chain->len) new_cap *= 2;
                moves = xrealloc(moves, new_cap * sizeof(*moves));
                move_cap = new_cap;
            }
            for (size_t i = 0; i < best_chain->len; i++) {
                moves[move_count++] = (CompactMove){
                    .source = best_chain->v[i],
                    .destination = destination + (uint32_t)i,
                };
            }
            if (planned_count == planned_cap) {
                size_t new_cap = planned_cap == 0 ? 16 : planned_cap * 2;
                planned = xrealloc(planned, new_cap * sizeof(*planned));
                planned_cap = new_cap;
            }
            planned[planned_count++] = (PlannedWholeObject){
                .is_dir = best_is_dir,
                .chain = best_chain,
                .path = best_path,
                .destination = destination,
            };
            if (best_is_root) selected_root = true;
            else selected_files[best_index] = true;
            destination += (uint32_t)best_chain->len;
            hole_remaining -= best_chain->len;
        }

        if (move_count != 0) {
            for (size_t i = 0; i < planned_count; i++) {
                fprintf(stderr, "compact-whole: %s %s (%zu clusters) -> cluster %" PRIu32 "\n",
                        planned[i].is_dir ? "DIR" : "FILE", planned[i].path,
                        planned[i].chain->len, planned[i].destination);
            }
            compact_execute_moves(fs, &dir_refs, journal_path, moves, move_count);
            stats.transactions++;
            stats.clusters_moved += move_count;
            stats.whole_clusters += move_count;
            stats.whole_objects += planned_count;
            fprintf(stderr,
                    "compact: moved %zu whole object%s / %zu clusters (total %zu); "
                    "terminal free run now %" PRIu64 " clusters\n",
                    planned_count, planned_count == 1 ? "" : "s", move_count,
                    stats.clusters_moved, terminal_free_clusters(fs));
            free(planned);
            free(moves);
            free(selected_files);
            u32vec_free(&root_chain);
            filelist_free(&files);
            dirreflist_free(&dir_refs);
            continue;
        }

        free(planned);
        free(moves);
        free(selected_files);

        /* A small hole immediately before a contiguous object cannot accept that
           whole object directly because the source and destination overlap. Move
           the object temporarily into the terminal free run. On the next pass its
           old allocation has merged with the hole, allowing whole-chain packing. */
        uint32_t source = hole_start + (uint32_t)hole_length;
        uint64_t terminal_count64 = terminal_free_clusters(fs);
        const U32Vec *stage_chain = NULL;
        const char *stage_path = NULL;
        bool stage_is_dir = false;
        if (source <= highest && cluster_is_movable_allocation(fs, source)) {
            if (root_chain.len != 0 && root_chain.v[0] == source &&
                chain_fragments(&root_chain) == 1) {
                stage_chain = &root_chain;
                stage_path = "<root directory>";
                stage_is_dir = true;
            } else {
                for (size_t i = 0; i < files.len; i++) {
                    const FileRecord *candidate = &files.v[i];
                    if (candidate->chain.len != 0 && candidate->chain.v[0] == source &&
                        candidate->fragments == 1) {
                        stage_chain = &candidate->chain;
                        stage_path = candidate->path;
                        stage_is_dir = candidate->is_dir;
                        break;
                    }
                }
            }
        }
        if (stage_chain != NULL && stage_chain->len <= terminal_count64 &&
            stage_chain->len <= remaining) {
            uint32_t terminal_start = fs->max_cluster - (uint32_t)terminal_count64 + 1;
            moves = xmalloc(stage_chain->len * sizeof(*moves));
            for (size_t i = 0; i < stage_chain->len; i++) {
                moves[i] = (CompactMove){
                    .source = stage_chain->v[i],
                    .destination = terminal_start + (uint32_t)i,
                };
            }
            fprintf(stderr,
                    "compact-stage: %s %s (%zu clusters) -> terminal cluster %" PRIu32
                    " to expand the low free run\n",
                    stage_is_dir ? "DIR" : "FILE", stage_path, stage_chain->len,
                    terminal_start);
            compact_execute_moves(fs, &dir_refs, journal_path, moves, stage_chain->len);
            stats.transactions++;
            stats.clusters_moved += stage_chain->len;
            stats.staged_objects++;
            stats.staged_clusters += stage_chain->len;
            fprintf(stderr,
                    "compact: staged one whole object / %zu clusters (total %zu); "
                    "terminal free run now %" PRIu64 " clusters\n",
                    stage_chain->len, stats.clusters_moved, terminal_free_clusters(fs));
            free(moves);
            u32vec_free(&root_chain);
            filelist_free(&files);
            dirreflist_free(&dir_refs);
            continue;
        }

        /* No complete chain fits and no contiguous object can be staged. Shift
           the next physical allocated extent downward without reversing or
           scattering its cluster order. */
        while (source <= highest && !cluster_is_movable_allocation(fs, source)) source++;
        if (source > highest) {
            fprintf(stderr,
                    "compact: cannot fill the free run at cluster %" PRIu32
                    " because no movable allocation follows it\n", hole_start);
            u32vec_free(&root_chain);
            filelist_free(&files);
            dirreflist_free(&dir_refs);
            break;
        }
        size_t source_run = 0;
        uint32_t c = source;
        while (c <= highest && cluster_is_movable_allocation(fs, c)) {
            source_run++;
            if (c == highest) break;
            c++;
        }
        size_t count = hole_length;
        if (count > source_run) count = source_run;
        if (count > batch_clusters) count = batch_clusters;
        if (count > remaining) count = remaining;
        if (count == 0) {
            u32vec_free(&root_chain);
            filelist_free(&files);
            dirreflist_free(&dir_refs);
            break;
        }

        moves = xmalloc(count * sizeof(*moves));
        for (size_t i = 0; i < count; i++) {
            moves[i] = (CompactMove){
                .source = source + (uint32_t)i,
                .destination = hole_start + (uint32_t)i,
            };
        }
        fprintf(stderr,
                "compact-extent: shifted %zu contiguous cluster%s from %" PRIu32
                " to %" PRIu32 " without reordering\n",
                count, count == 1 ? "" : "s", source, hole_start);
        compact_execute_moves(fs, &dir_refs, journal_path, moves, count);
        stats.transactions++;
        stats.clusters_moved += count;
        stats.extent_clusters += count;
        stats.extent_transactions++;
        if (count == 1) stats.singleton_transactions++;
        fprintf(stderr,
                "compact: moved ordered extent of %zu cluster%s (total %zu); "
                "terminal free run now %" PRIu64 " clusters\n",
                count, count == 1 ? "" : "s", stats.clusters_moved,
                terminal_free_clusters(fs));
        free(moves);
        u32vec_free(&root_chain);
        filelist_free(&files);
        dirreflist_free(&dir_refs);
    }

    update_fsinfo_next_free(fs, first_free_cluster_hint(fs));
    fat32_sync(fs);
    return stats;
}

static void copy_file_to_run(Fat32 *fs, const FileRecord *file, uint32_t dest_start,
                             const char *journal_path) {
    Journal j = {
        .device_path = xstrdup(fs->dev.path),
        .volume_id = fs->volume_id,
        .stage = J_PREPARED,
        .dirent_offset = file->dirent_offset,
        .old_first = file->first_cluster,
        .dest_start = dest_start,
    };
    for (size_t i = 0; i < file->chain.len; i++) u32vec_push(&j.source, file->chain.v[i]);
    journal_write(journal_path, &j);

    uint32_t *destinations = xmalloc(file->chain.len * sizeof(*destinations));
    for (size_t i = 0; i < file->chain.len; i++) {
        destinations[i] = dest_start + (uint32_t)i;
    }
    copy_cluster_mapping_buffered(fs, file->chain.v, destinations, file->chain.len);
    free(destinations);
    fat32_sync(fs);
    j.stage = J_DATA_COPIED;
    journal_write(journal_path, &j);

    FatUpdate *updates = xmalloc(file->chain.len * sizeof(*updates));
    for (size_t i = 0; i < file->chain.len; i++) {
        uint32_t dest = dest_start + (uint32_t)i;
        uint32_t next = (i + 1 == file->chain.len) ? fat_eoc_value(fs) : dest + 1;
        updates[i] = (FatUpdate){.cluster = dest, .value = next};
    }
    fat32_apply_updates(fs, updates, file->chain.len);
    free(updates);
    fat32_sync(fs);
    j.stage = J_DEST_LINKED;
    journal_write(journal_path, &j);

    write_dirent_first_cluster(fs, file->dirent_offset, dest_start);
    fat32_sync(fs);
    j.stage = J_SWITCHED;
    journal_write(journal_path, &j);

    free_cluster_list(fs, file->chain.v, file->chain.len);
    update_fsinfo_next_free(fs, file->chain.v[0]);
    fat32_sync(fs);
    j.stage = J_OLD_FREED;
    journal_write(journal_path, &j);

    journal_remove(journal_path);
    journal_free(&j);
}

static int compare_files_desc(const void *a, const void *b) {
    const FileRecord *fa = a;
    const FileRecord *fb = b;
    if (fa->is_dir != fb->is_dir) return fa->is_dir ? 1 : -1;
    if (fa->fragments != fb->fragments) return fa->fragments < fb->fragments ? 1 : -1;
    if (fa->chain.len != fb->chain.len) return fa->chain.len < fb->chain.len ? 1 : -1;
    return strcmp(fa->path, fb->path);
}

static U32Vec filesystem_root_chain(Fat32 *fs) {
    if (fs->root_is_fixed) return (U32Vec){0};
    return fat32_read_chain(fs, fs->root_cluster);
}

static size_t filesystem_root_fragments(Fat32 *fs) {
    if (fs->root_is_fixed) return 1;
    U32Vec chain = fat32_read_chain(fs, fs->root_cluster);
    size_t n = chain_fragments(&chain);
    u32vec_free(&chain);
    return n;
}

static void print_analysis(Fat32 *fs, const FileList *files) {
    uint64_t regular = 0, dirs = 0, fragmented = 0, dir_fragmented = 0;
    uint64_t allocated_clusters = 0;
    size_t worst_fragments = 0;
    const char *worst_path = NULL;
    for (size_t i = 0; i < files->len; i++) {
        const FileRecord *f = &files->v[i];
        allocated_clusters += f->chain.len;
        if (f->is_dir) {
            dirs++;
            if (f->fragments > 1) dir_fragmented++;
        } else {
            regular++;
            if (f->fragments > 1) fragmented++;
        }
        if (f->fragments > worst_fragments) {
            worst_fragments = f->fragments;
            worst_path = f->path;
        }
    }
    U32Vec root_chain = filesystem_root_chain(fs);
    size_t root_frags = filesystem_root_fragments(fs);
    allocated_clusters += root_chain.len;
    if (root_frags > worst_fragments) {
        worst_fragments = root_frags;
        worst_path = "<root directory>";
    }
    uint64_t free_clusters = count_free_clusters(fs);

    printf("%s volume ID:        %08" PRIx32 "\n", fat_type_name(fs), fs->volume_id);
    printf("Bytes per sector:       %u\n", fs->bytes_per_sector);
    printf("Sectors per cluster:    %u\n", fs->sectors_per_cluster);
    printf("Cluster size:           %" PRIu64 " bytes\n", fs->cluster_size);
    printf("Data clusters:          %" PRIu32 "\n", fs->cluster_count);
    printf("Free clusters:          %" PRIu64 "\n", free_clusters);
    printf("Scanned regular files:  %" PRIu64 "\n", regular);
    printf("Fragmented files:       %" PRIu64 "\n", fragmented);
    printf("Scanned directories:    %" PRIu64 "\n", dirs + 1);
    printf("Fragmented directories: %" PRIu64 " (root fragments: %zu)\n", dir_fragmented, root_frags);
    printf("Referenced clusters:    %" PRIu64 "\n", allocated_clusters);
    if (worst_path != NULL) printf("Worst chain:             %zu fragments: %s\n", worst_fragments, worst_path);
    u32vec_free(&root_chain);
}

static void list_fragmented(Fat32 *fs, const FileList *files) {
    U32Vec root_chain = filesystem_root_chain(fs);
    size_t root_fragments = filesystem_root_fragments(fs);
    if (root_fragments > 1) {
        printf("DIR   %6zu clusters  %4zu fragments  <root directory>\n",
               root_chain.len, root_fragments);
    }
    u32vec_free(&root_chain);

    for (size_t i = 0; i < files->len; i++) {
        const FileRecord *f = &files->v[i];
        if (f->fragments > 1) {
            printf("%-4s  %6zu clusters  %4zu fragments  %s\n",
                   f->is_dir ? "DIR" : "FILE", f->chain.len, f->fragments, f->path);
        }
    }
}


typedef enum {
    MAP_CLUSTER_USED = 1u << 0,
    MAP_CLUSTER_FRAGMENTED = 1u << 1,
    MAP_CLUSTER_DIRECTORY = 1u << 2,
    MAP_CLUSTER_BAD = 1u << 3
} MapClusterFlag;

static void json_print_string(const char *value) {
    putchar('"');
    for (const unsigned char *p = (const unsigned char *)value; *p != '\0'; p++) {
        switch (*p) {
            case '"': fputs("\\\"", stdout); break;
            case '\\': fputs("\\\\", stdout); break;
            case '\b': fputs("\\b", stdout); break;
            case '\f': fputs("\\f", stdout); break;
            case '\n': fputs("\\n", stdout); break;
            case '\r': fputs("\\r", stdout); break;
            case '\t': fputs("\\t", stdout); break;
            default:
                if (*p < 0x20) printf("\\u%04x", (unsigned)*p);
                else putchar((int)*p);
                break;
        }
    }
    putchar('"');
}

static void mark_map_chain(uint8_t *flags, const U32Vec *chain, bool directory,
                           bool fragmented) {
    uint8_t value = MAP_CLUSTER_USED;
    if (directory) value |= MAP_CLUSTER_DIRECTORY;
    if (fragmented) value |= MAP_CLUSTER_FRAGMENTED;
    for (size_t i = 0; i < chain->len; i++) flags[chain->v[i]] |= value;
}

static void print_map_json(Fat32 *fs, const FileList *files, size_t requested_cells) {
    size_t cells = requested_cells;
    if (cells == 0) cells = 4096;
    if (cells > fs->cluster_count) cells = fs->cluster_count;
    if (cells == 0) cells = 1;

    uint8_t *flags = xcalloc((size_t)fs->max_cluster + 1, sizeof(*flags));
    uint64_t regular = 0, directories = 1, fragmented_files = 0, fragmented_dirs = 0;
    size_t worst_fragments = 0;
    const char *worst_path = NULL;

    for (uint32_t c = 2; c <= fs->max_cluster; c++) {
        uint32_t v = fat_value(fs, c);
        if (v == 0) continue;
        flags[c] |= MAP_CLUSTER_USED;
        if (v == fat_bad_value(fs) || (v >= fat_reserved_min(fs) && v < fat_eoc_min(fs))) {
            flags[c] |= MAP_CLUSTER_BAD;
        }
    }

    U32Vec root_chain = filesystem_root_chain(fs);
    size_t root_fragments = filesystem_root_fragments(fs);
    mark_map_chain(flags, &root_chain, true, root_fragments > 1);
    if (root_fragments > worst_fragments) {
        worst_fragments = root_fragments;
        worst_path = "<root directory>";
    }

    for (size_t i = 0; i < files->len; i++) {
        const FileRecord *f = &files->v[i];
        if (f->is_dir) {
            directories++;
            if (f->fragments > 1) fragmented_dirs++;
        } else {
            regular++;
            if (f->fragments > 1) fragmented_files++;
        }
        mark_map_chain(flags, &f->chain, f->is_dir, f->fragments > 1);
        if (f->fragments > worst_fragments) {
            worst_fragments = f->fragments;
            worst_path = f->path;
        }
    }

    uint64_t free_clusters = count_free_clusters(fs);
    uint32_t highest = 1;
    uint64_t gaps = holes_below_high_water(fs, &highest);
    uint64_t terminal = terminal_free_clusters(fs);

    fputs("{\n", stdout);
    fputs("  \"program\": \"fat32defrag\",\n", stdout);
    printf("  \"version\": \"%s\",\n", PROGRAM_VERSION);
    fputs("  \"device\": ", stdout); json_print_string(fs->dev.path); fputs(",\n", stdout);
    printf("  \"filesystem\": \"%s\",\n", fat_type_name(fs));
    printf("  \"volume_id\": \"%08" PRIx32 "\",\n", fs->volume_id);
    printf("  \"bytes_per_sector\": %u,\n", fs->bytes_per_sector);
    printf("  \"sectors_per_cluster\": %u,\n", fs->sectors_per_cluster);
    printf("  \"cluster_size\": %" PRIu64 ",\n", fs->cluster_size);
    printf("  \"data_clusters\": %" PRIu32 ",\n", fs->cluster_count);
    printf("  \"free_clusters\": %" PRIu64 ",\n", free_clusters);
    printf("  \"used_clusters\": %" PRIu64 ",\n", (uint64_t)fs->cluster_count - free_clusters);
    printf("  \"regular_files\": %" PRIu64 ",\n", regular);
    printf("  \"fragmented_files\": %" PRIu64 ",\n", fragmented_files);
    printf("  \"directories\": %" PRIu64 ",\n", directories);
    printf("  \"fragmented_directories\": %" PRIu64 ",\n", fragmented_dirs);
    printf("  \"root_fragments\": %zu,\n", root_fragments);
    printf("  \"worst_fragments\": %zu,\n", worst_fragments);
    fputs("  \"worst_path\": ", stdout);
    if (worst_path == NULL) fputs("null", stdout); else json_print_string(worst_path);
    fputs(",\n", stdout);
    printf("  \"highest_allocated_cluster\": %" PRIu32 ",\n", highest);
    printf("  \"free_gaps_below_highest\": %" PRIu64 ",\n", gaps);
    printf("  \"terminal_free_clusters\": %" PRIu64 ",\n", terminal);
    printf("  \"cell_count\": %zu,\n", cells);
    fputs("  \"cells\": [\n", stdout);

    for (size_t i = 0; i < cells; i++) {
        uint64_t first_index = ((uint64_t)i * fs->cluster_count) / cells;
        uint64_t end_index = ((uint64_t)(i + 1) * fs->cluster_count) / cells;
        if (end_index <= first_index) end_index = first_index + 1;
        uint32_t start = (uint32_t)(first_index + 2);
        uint32_t end = (uint32_t)(end_index + 1);
        if (end > fs->max_cluster) end = fs->max_cluster;
        uint64_t free_count = 0, used_count = 0, fragmented_count = 0;
        uint64_t directory_count = 0, bad_count = 0;
        for (uint32_t c = start; c <= end; c++) {
            uint8_t state = flags[c];
            if ((state & MAP_CLUSTER_USED) == 0) free_count++;
            else used_count++;
            if ((state & MAP_CLUSTER_FRAGMENTED) != 0) fragmented_count++;
            if ((state & MAP_CLUSTER_DIRECTORY) != 0) directory_count++;
            if ((state & MAP_CLUSTER_BAD) != 0) bad_count++;
        }
        printf("    {\"start\":%" PRIu32 ",\"end\":%" PRIu32
               ",\"free\":%" PRIu64 ",\"used\":%" PRIu64
               ",\"fragmented\":%" PRIu64 ",\"directory\":%" PRIu64
               ",\"bad\":%" PRIu64 "}%s\n",
               start, end, free_count, used_count, fragmented_count,
               directory_count, bad_count, i + 1 == cells ? "" : ",");
    }
    fputs("  ]\n}\n", stdout);

    free(flags);
    u32vec_free(&root_chain);
}

static LiveMapCell *build_live_map_cells(Fat32 *fs, const FileList *files, size_t requested_cells,
                                         size_t *actual_cells, uint64_t *fragmented_files,
                                         uint64_t *fragmented_dirs, uint64_t *free_clusters,
                                         uint64_t *free_gaps) {
    size_t cells = requested_cells == 0 ? 4096 : requested_cells;
    if (cells > fs->cluster_count) cells = fs->cluster_count;
    if (cells == 0) cells = 1;
    uint8_t *flags = xcalloc((size_t)fs->max_cluster + 1, sizeof(*flags));
    uint64_t ff = 0, fd = 0;
    for (uint32_t c = 2; c <= fs->max_cluster; c++) {
        uint32_t v = fat_value(fs, c);
        if (v == 0) continue;
        flags[c] |= MAP_CLUSTER_USED;
        if (v == fat_bad_value(fs) || (v >= fat_reserved_min(fs) && v < fat_eoc_min(fs)))
            flags[c] |= MAP_CLUSTER_BAD;
    }
    U32Vec root_chain = filesystem_root_chain(fs);
    size_t root_fragments = filesystem_root_fragments(fs);
    mark_map_chain(flags, &root_chain, true, root_fragments > 1);
    if (root_fragments > 1) fd++;
    for (size_t i = 0; i < files->len; i++) {
        const FileRecord *f = &files->v[i];
        if (f->is_dir) { if (f->fragments > 1) fd++; }
        else { if (f->fragments > 1) ff++; }
        mark_map_chain(flags, &f->chain, f->is_dir, f->fragments > 1);
    }
    LiveMapCell *out = xcalloc(cells, sizeof(*out));
    for (size_t i = 0; i < cells; i++) {
        uint64_t first_index = ((uint64_t)i * fs->cluster_count) / cells;
        uint64_t end_index = ((uint64_t)(i + 1) * fs->cluster_count) / cells;
        if (end_index <= first_index) end_index = first_index + 1;
        uint32_t start = (uint32_t)(first_index + 2);
        uint32_t end = (uint32_t)(end_index + 1);
        if (end > fs->max_cluster) end = fs->max_cluster;
        out[i].start = start; out[i].end = end;
        for (uint32_t c = start; c <= end; c++) {
            uint8_t state = flags[c];
            if ((state & MAP_CLUSTER_USED) == 0) out[i].free_count++;
            else out[i].used_count++;
            if ((state & MAP_CLUSTER_FRAGMENTED) != 0) out[i].fragmented_count++;
            if ((state & MAP_CLUSTER_DIRECTORY) != 0) out[i].directory_count++;
            if ((state & MAP_CLUSTER_BAD) != 0) out[i].bad_count++;
        }
    }
    free(flags);
    u32vec_free(&root_chain);
    uint32_t highest = 1;
    if (actual_cells) *actual_cells = cells;
    if (fragmented_files) *fragmented_files = ff;
    if (fragmented_dirs) *fragmented_dirs = fd;
    if (free_clusters) *free_clusters = count_free_clusters(fs);
    if (free_gaps) *free_gaps = holes_below_high_water(fs, &highest);
    return out;
}

static void initialise_live_map(Fat32 *fs, const FileList *files) {
    if (g_live_map_cells == 0) return;
    free(g_live_map_previous);
    g_live_map_previous = build_live_map_cells(fs, files, g_live_map_cells,
                                               &g_live_map_previous_count, NULL, NULL, NULL, NULL);
}

static void emit_live_map_update(Fat32 *fs) {
    if (g_live_map_cells == 0) return;
    DirRefList refs = {0};
    FileList files = scan_files(fs, &refs);
    size_t count = 0;
    uint64_t fragmented_files = 0, fragmented_dirs = 0, free_clusters = 0, free_gaps = 0;
    LiveMapCell *now = build_live_map_cells(fs, &files, g_live_map_cells, &count,
                                            &fragmented_files, &fragmented_dirs,
                                            &free_clusters, &free_gaps);
    fputs("@@LIVE_MAP {\"fragmented_files\":", stderr);
    fprintf(stderr, "%" PRIu64 ",\"fragmented_directories\":%" PRIu64
                    ",\"free_clusters\":%" PRIu64 ",\"free_gaps_below_highest\":%" PRIu64
                    ",\"cells\":[",
            fragmented_files, fragmented_dirs, free_clusters, free_gaps);
    bool first = true;
    for (size_t i = 0; i < count; i++) {
        bool changed = g_live_map_previous == NULL || i >= g_live_map_previous_count ||
                       memcmp(&now[i], &g_live_map_previous[i], sizeof(now[i])) != 0;
        if (!changed) continue;
        fprintf(stderr, "%s{\"i\":%zu,\"start\":%" PRIu32 ",\"end\":%" PRIu32
                        ",\"free\":%" PRIu64 ",\"used\":%" PRIu64
                        ",\"fragmented\":%" PRIu64 ",\"directory\":%" PRIu64
                        ",\"bad\":%" PRIu64 "}",
                first ? "" : ",", i, now[i].start, now[i].end, now[i].free_count,
                now[i].used_count, now[i].fragmented_count, now[i].directory_count,
                now[i].bad_count);
        first = false;
    }
    fputs("]}\n", stderr);
    fflush(stderr);
    free(g_live_map_previous);
    g_live_map_previous = now;
    g_live_map_previous_count = count;
    filelist_free(&files);
    dirreflist_free(&refs);
}

static bool better_directory_candidate(size_t fragments, size_t clusters, const char *path,
                                       size_t best_fragments, size_t best_clusters,
                                       const char *best_path) {
    if (best_path == NULL) return true;
    if (fragments != best_fragments) return fragments > best_fragments;
    if (clusters != best_clusters) return clusters > best_clusters;
    return strcmp(path, best_path) < 0;
}

static size_t defrag_directories(Fat32 *fs, const char *journal_path,
                                 size_t max_directories) {
    size_t moved = 0;
    for (;;) {
        if (g_stop_requested) {
            fprintf(stderr, "interrupt requested; stopping directory defrag between moves\n");
            break;
        }
        if (max_directories != 0 && moved >= max_directories) break;

        DirRefList dir_refs = {0};
        FileList files = scan_files(fs, &dir_refs);
        U32Vec root_chain = filesystem_root_chain(fs);
        size_t root_fragments = filesystem_root_fragments(fs);

        bool best_is_root = false;
        const FileRecord *best = NULL;
        const U32Vec *best_chain = NULL;
        const char *best_path = NULL;
        size_t best_fragments = 0;
        size_t best_clusters = 0;
        uint32_t best_destination = 0;

        if (root_fragments > 1) {
            uint32_t destination = 0;
            if (find_free_run(fs, root_chain.len, &destination)) {
                best_is_root = true;
                best_chain = &root_chain;
                best_path = "<root directory>";
                best_fragments = root_fragments;
                best_clusters = root_chain.len;
                best_destination = destination;
            }
        }

        for (size_t i = 0; i < files.len; i++) {
            const FileRecord *candidate = &files.v[i];
            if (!candidate->is_dir || candidate->chain.len < 2 || candidate->fragments <= 1) continue;
            uint32_t destination = 0;
            if (!find_free_run(fs, candidate->chain.len, &destination)) continue;
            if (better_directory_candidate(candidate->fragments, candidate->chain.len,
                                           candidate->path, best_fragments, best_clusters,
                                           best_path)) {
                best_is_root = false;
                best = candidate;
                best_chain = &candidate->chain;
                best_path = candidate->path;
                best_fragments = candidate->fragments;
                best_clusters = candidate->chain.len;
                best_destination = destination;
            }
        }

        if (best_chain == NULL) {
            bool fragmented_remains = root_fragments > 1;
            for (size_t i = 0; i < files.len && !fragmented_remains; i++) {
                fragmented_remains = files.v[i].is_dir && files.v[i].fragments > 1;
            }
            if (fragmented_remains) {
                fprintf(stderr, "skip: no free contiguous run large enough for any fragmented directory\n");
            }
            u32vec_free(&root_chain);
            filelist_free(&files);
            dirreflist_free(&dir_refs);
            break;
        }

        CompactMove *moves = xmalloc(best_chain->len * sizeof(*moves));
        for (size_t i = 0; i < best_chain->len; i++) {
            moves[i] = (CompactMove){
                .source = best_chain->v[i],
                .destination = best_destination + (uint32_t)i,
            };
        }
        uint32_t *pred = build_predecessor_table(fs);
        CompactJournal journal = make_compact_journal(fs, &dir_refs, pred, moves, best_chain->len);
        free(pred);
        free(moves);

        fprintf(stderr, "move-dir: %s (%zu clusters, %zu fragments) -> cluster %" PRIu32 "\n",
                best_is_root ? "<root directory>" : best->path,
                best_chain->len, best_fragments, best_destination);
        compact_journal_write(journal_path, &journal);
        complete_compact_journal(fs, journal_path, &journal);
        compact_journal_free(&journal);
        emit_live_map_update(fs);
        moved++;

        u32vec_free(&root_chain);
        filelist_free(&files);
        dirreflist_free(&dir_refs);
    }
    return moved;
}

static size_t defrag_files_single_transaction(Fat32 *fs, FileList *files,
                                              const char *journal_path,
                                              size_t max_files) {
    size_t moved = 0;
    for (size_t i = 0; i < files->len; i++) {
        if (g_stop_requested) {
            fprintf(stderr, "interrupt requested; stopping file defrag between moves\n");
            break;
        }
        FileRecord *f = &files->v[i];
        if (f->is_dir || f->chain.len < 2 || f->fragments <= 1) continue;
        if (max_files != 0 && moved >= max_files) break;
        uint32_t dest = 0;
        if (!find_free_run(fs, f->chain.len, &dest)) {
            fprintf(stderr, "skip: no free run of %zu clusters for %s\n", f->chain.len, f->path);
            continue;
        }
        fprintf(stderr, "move: %s (%zu clusters, %zu fragments) -> cluster %" PRIu32 "\n",
                f->path, f->chain.len, f->fragments, dest);
        copy_file_to_run(fs, f, dest, journal_path);
        emit_live_map_update(fs);
        moved++;
    }
    return moved;
}

typedef struct {
    FileRecord *file;
    uint32_t destination;
} PlannedFileMove;

typedef struct {
    size_t files_moved;
    size_t clusters_moved;
    size_t transactions;
} FileDefragStats;

static FileDefragStats defrag_files_batched(Fat32 *fs, FileList *files,
                                            const DirRefList *dir_refs,
                                            const char *journal_path,
                                            size_t max_files,
                                            size_t transaction_files) {
    FileDefragStats stats = {0};
    size_t cursor = 0;

    while (cursor < files->len) {
        if (g_stop_requested) {
            fprintf(stderr, "interrupt requested; stopping file defrag between transactions\n");
            break;
        }
        if (max_files != 0 && stats.files_moved >= max_files) break;

        size_t batch_limit = transaction_files;
        size_t remaining_records = files->len - cursor;
        if (batch_limit > remaining_records) batch_limit = remaining_records;
        if (max_files != 0 && batch_limit > max_files - stats.files_moved) {
            batch_limit = max_files - stats.files_moved;
        }
        if (batch_limit == 0) break;

        uint8_t *reserved = xcalloc((size_t)fs->max_cluster + 1, 1);
        PlannedFileMove *planned = xmalloc(batch_limit * sizeof(*planned));
        size_t planned_count = 0;
        CompactMove *moves = NULL;
        size_t move_count = 0;
        size_t move_cap = 0;

        while (cursor < files->len && planned_count < batch_limit) {
            FileRecord *f = &files->v[cursor++];
            if (f->is_dir || f->chain.len < 2 || f->fragments <= 1) continue;

            uint32_t destination = 0;
            if (!find_free_run_reserved(fs, reserved, f->chain.len, &destination)) {
                fprintf(stderr, "skip: no free run of %zu clusters for %s\n",
                        f->chain.len, f->path);
                continue;
            }
            for (size_t i = 0; i < f->chain.len; i++) {
                reserved[destination + (uint32_t)i] = 1;
            }

            if (f->chain.len > SIZE_MAX - move_count) {
                free(reserved);
                free(planned);
                free(moves);
                die_msg("defrag transaction cluster count overflow");
            }
            size_t required = move_count + f->chain.len;
            if (required > move_cap) {
                size_t new_cap = move_cap == 0 ? 1024 : move_cap;
                while (new_cap < required) {
                    if (new_cap > SIZE_MAX / 2) {
                        new_cap = required;
                        break;
                    }
                    new_cap *= 2;
                }
                if (new_cap > SIZE_MAX / sizeof(*moves)) {
                    free(reserved);
                    free(planned);
                    free(moves);
                    die_msg("defrag transaction allocation overflow");
                }
                moves = xrealloc(moves, new_cap * sizeof(*moves));
                move_cap = new_cap;
            }
            for (size_t i = 0; i < f->chain.len; i++) {
                moves[move_count++] = (CompactMove){
                    .source = f->chain.v[i],
                    .destination = destination + (uint32_t)i,
                };
            }
            planned[planned_count++] = (PlannedFileMove){
                .file = f,
                .destination = destination,
            };
        }
        free(reserved);

        if (planned_count == 0) {
            free(planned);
            free(moves);
            continue;
        }
        if (g_stop_requested) {
            fprintf(stderr, "interrupt requested; discarding the uncommitted file batch\n");
            free(planned);
            free(moves);
            break;
        }

        fprintf(stderr, "defrag-batch: %zu file%s, %zu clusters in one journal transaction\n",
                planned_count, planned_count == 1 ? "" : "s", move_count);
        for (size_t i = 0; i < planned_count; i++) {
            const PlannedFileMove *p = &planned[i];
            fprintf(stderr, "move: %s (%zu clusters, %zu fragments) -> cluster %" PRIu32 "\n",
                    p->file->path, p->file->chain.len, p->file->fragments,
                    p->destination);
        }
        compact_execute_moves(fs, dir_refs, journal_path, moves, move_count);
        stats.files_moved += planned_count;
        stats.clusters_moved += move_count;
        stats.transactions++;
        fprintf(stderr,
                "defrag-batch: committed %zu file%s; total %zu file%s in %zu transaction%s\n",
                planned_count, planned_count == 1 ? "" : "s",
                stats.files_moved, stats.files_moved == 1 ? "" : "s",
                stats.transactions, stats.transactions == 1 ? "" : "s");
        free(planned);
        free(moves);
    }
    return stats;
}

static FileDefragStats defrag_files(Fat32 *fs, FileList *files,
                                    const DirRefList *dir_refs,
                                    const char *journal_path,
                                    size_t max_files,
                                    size_t transaction_files) {
    qsort(files->v, files->len, sizeof(files->v[0]), compare_files_desc);
    if (transaction_files <= 1) {
        size_t moved = defrag_files_single_transaction(fs, files, journal_path, max_files);
        return (FileDefragStats){
            .files_moved = moved,
            .transactions = moved,
        };
    }
    return defrag_files_batched(fs, files, dir_refs, journal_path,
                                max_files, transaction_files);
}

static void usage(FILE *out) {
    fprintf(out,
        "Usage:\n"
        "  %s analyze DEVICE [--list]\n"
        "  %s map DEVICE [--cells N]\n"
        "  %s defrag DEVICE --write --confirm DEVICE [--journal PATH]\n"
        "       [--max-files N] [--max-directories N] [--files-only|--directories-only]\n"
        "       [--transaction-files N] [--ram-buffer auto|SIZE] [--workers auto|N]\n"
        "  %s compact DEVICE --write --confirm DEVICE [--journal PATH]\n"
        "       [--max-clusters N] [--max-transactions N] [--batch-clusters N]\n"
        "       [--ram-buffer auto|SIZE] [--workers auto|N]\n"
        "  %s recover DEVICE --write --confirm DEVICE [--journal PATH]\n"
        "       [--ram-buffer auto|SIZE] [--workers auto|N]\n\n"
        "DEVICE may be an unmounted block-device partition or a regular FAT12/FAT16/FAT32 image.\n"
        "The defrag command relocates fragmented directory chains and regular files into\n"
        "genuinely free contiguous cluster runs. Directory moves update parent entries,\n"
        "`.` and `..` references, and FAT32 root boot-sector fields. The compact command\n"
        "packs complete file and directory chains toward the start whenever they fit.\n"
        "For unavoidable small holes it shifts ordered physical extents downward without\n"
        "reversing or scattering them, leaving the largest possible terminal free run.\n"
        "SIZE accepts byte suffixes such as 512M, 2G, or 8GiB. Automatic mode uses up\n"
        "to one quarter of currently available RAM, capped at 8 GiB. Regular files are\n"
        "committed in recoverable batches of 32 by default. Ctrl-C requests a clean stop\n"
        "after the current journalled file batch or compaction transaction.\n",
        PROGRAM_NAME, PROGRAM_NAME, PROGRAM_NAME, PROGRAM_NAME, PROGRAM_NAME);
}

static size_t parse_size(const char *s) {
    char *end = NULL;
    errno = 0;
    unsigned long long v = strtoull(s, &end, 10);
    if (errno != 0 || end == s || *end != '\0' || v > SIZE_MAX) die_msg("invalid numeric argument");
    return (size_t)v;
}

static size_t parse_byte_size(const char *s) {
    char *end = NULL;
    errno = 0;
    unsigned long long value = strtoull(s, &end, 10);
    if (errno != 0 || end == s) die_msg("invalid RAM buffer size");
    char suffix[8] = {0};
    size_t suffix_len = strlen(end);
    if (suffix_len >= sizeof(suffix)) die_msg("invalid RAM buffer suffix");
    for (size_t i = 0; i < suffix_len; i++) suffix[i] = (char)toupper((unsigned char)end[i]);

    uint64_t multiplier = 1;
    if (suffix[0] == '\0' || strcmp(suffix, "B") == 0) multiplier = 1;
    else if (strcmp(suffix, "K") == 0 || strcmp(suffix, "KB") == 0 || strcmp(suffix, "KIB") == 0) {
        multiplier = UINT64_C(1024);
    } else if (strcmp(suffix, "M") == 0 || strcmp(suffix, "MB") == 0 || strcmp(suffix, "MIB") == 0) {
        multiplier = UINT64_C(1024) * 1024;
    } else if (strcmp(suffix, "G") == 0 || strcmp(suffix, "GB") == 0 || strcmp(suffix, "GIB") == 0) {
        multiplier = UINT64_C(1024) * 1024 * 1024;
    } else if (strcmp(suffix, "T") == 0 || strcmp(suffix, "TB") == 0 || strcmp(suffix, "TIB") == 0) {
        multiplier = UINT64_C(1024) * 1024 * 1024 * 1024;
    } else {
        die_msg("invalid RAM buffer suffix; use K, M, G, or T");
    }
    if (value > UINT64_MAX / multiplier) die_msg("RAM buffer size is too large");
    uint64_t bytes = (uint64_t)value * multiplier;
    if (bytes == 0 || bytes > SIZE_MAX) die_msg("RAM buffer size is outside this build's range");
    return (size_t)bytes;
}

static void install_signal_handlers(void) {
    struct sigaction sa;
    memset(&sa, 0, sizeof(sa));
    sa.sa_handler = request_stop;
    sigemptyset(&sa.sa_mask);
    if (sigaction(SIGINT, &sa, NULL) != 0) die_errno("install SIGINT handler");
    if (sigaction(SIGTERM, &sa, NULL) != 0) die_errno("install SIGTERM handler");
}

static void print_io_configuration(void) {
    double mib = (double)g_io.ram_limit / (1024.0 * 1024.0);
    printf("RAM I/O buffer:          %.1f MiB\n", mib);
    printf("Source read workers:     %zu\n", g_io.workers);
    printf("Rotational target:       %s\n", g_io.rotational ? "yes" : "no");
}

static void print_io_statistics(void) {
    double read_mib = (double)g_io.bytes_read / (1024.0 * 1024.0);
    double write_mib = (double)g_io.bytes_written / (1024.0 * 1024.0);
    printf("Buffered data read:      %.1f MiB in %" PRIu64 " extent%s\n",
           read_mib, g_io.read_extents, g_io.read_extents == 1 ? "" : "s");
    printf("Buffered data written:   %.1f MiB in %" PRIu64 " extent%s\n",
           write_mib, g_io.write_extents, g_io.write_extents == 1 ? "" : "s");
}

int main(int argc, char **argv) {
    if (argc == 2 && strcmp(argv[1], "--version") == 0) {
        printf("%s %s\n", PROGRAM_NAME, PROGRAM_VERSION);
        return EXIT_SUCCESS;
    }
    if (argc < 3) {
        usage(stderr);
        return EXIT_FAILURE;
    }
    const char *command = argv[1];
    const char *device_path = argv[2];
    bool write_flag = false;
    const char *confirm = NULL;
    const char *journal_arg = NULL;
    bool list = false;
    size_t map_cells = 4096;
    size_t live_map_cells = 0;
    size_t max_files = 0;
    size_t max_directories = 0;
    size_t max_clusters = 0;
    size_t max_transactions = 0;
    size_t batch_clusters = 4096;
    size_t transaction_files = 32;
    bool files_only = false;
    bool directories_only = false;
    const char *ram_buffer_arg = "auto";
    const char *workers_arg = "auto";

    for (int i = 3; i < argc; i++) {
        if (strcmp(argv[i], "--write") == 0) write_flag = true;
        else if (strcmp(argv[i], "--list") == 0) list = true;
        else if (strcmp(argv[i], "--cells") == 0 && i + 1 < argc) {
            map_cells = parse_size(argv[++i]);
            if (map_cells == 0 || map_cells > 1048576) {
                die_msg("--cells must be between 1 and 1048576");
            }
        }
        else if (strcmp(argv[i], "--live-map-cells") == 0 && i + 1 < argc) {
            live_map_cells = parse_size(argv[++i]);
            if (live_map_cells == 0 || live_map_cells > 1048576)
                die_msg("--live-map-cells must be between 1 and 1048576");
        }
        else if (strcmp(argv[i], "--confirm") == 0 && i + 1 < argc) confirm = argv[++i];
        else if (strcmp(argv[i], "--journal") == 0 && i + 1 < argc) journal_arg = argv[++i];
        else if (strcmp(argv[i], "--max-files") == 0 && i + 1 < argc) max_files = parse_size(argv[++i]);
        else if (strcmp(argv[i], "--max-directories") == 0 && i + 1 < argc) {
            max_directories = parse_size(argv[++i]);
        }
        else if (strcmp(argv[i], "--files-only") == 0) files_only = true;
        else if (strcmp(argv[i], "--directories-only") == 0) directories_only = true;
        else if (strcmp(argv[i], "--max-clusters") == 0 && i + 1 < argc) {
            max_clusters = parse_size(argv[++i]);
        }
        else if (strcmp(argv[i], "--max-transactions") == 0 && i + 1 < argc) {
            max_transactions = parse_size(argv[++i]);
        }
        else if (strcmp(argv[i], "--batch-clusters") == 0 && i + 1 < argc) {
            batch_clusters = parse_size(argv[++i]);
            if (batch_clusters == 0) die_msg("--batch-clusters must be at least 1");
        }
        else if (strcmp(argv[i], "--transaction-files") == 0 && i + 1 < argc) {
            transaction_files = parse_size(argv[++i]);
            if (transaction_files == 0) die_msg("--transaction-files must be at least 1");
        }
        else if (strcmp(argv[i], "--ram-buffer") == 0 && i + 1 < argc) {
            ram_buffer_arg = argv[++i];
        }
        else if (strcmp(argv[i], "--workers") == 0 && i + 1 < argc) {
            workers_arg = argv[++i];
        }
        else if (strcmp(argv[i], "--version") == 0) {
            printf("%s %s\n", PROGRAM_NAME, PROGRAM_VERSION);
            return EXIT_SUCCESS;
        } else {
            usage(stderr);
            return EXIT_FAILURE;
        }
    }

    if (files_only && directories_only) die_msg("--files-only and --directories-only are mutually exclusive");
    if ((files_only || directories_only || max_directories != 0) && strcmp(command, "defrag") != 0) {
        die_msg("directory-selection options are valid only with defrag");
    }
    if (max_transactions != 0 && strcmp(command, "compact") != 0) {
        die_msg("--max-transactions is valid only with compact");
    }
    if (map_cells != 4096 && strcmp(command, "map") != 0) {
        die_msg("--cells is valid only with map");
    }
    if (live_map_cells != 0 && strcmp(command, "defrag") != 0 && strcmp(command, "compact") != 0) {
        die_msg("--live-map-cells is valid only with defrag or compact");
    }
    g_live_map_cells = live_map_cells;

    bool mutating = strcmp(command, "defrag") == 0 || strcmp(command, "compact") == 0 ||
                    strcmp(command, "recover") == 0;
    if (mutating && (!write_flag || confirm == NULL || strcmp(confirm, device_path) != 0)) {
        die_msg("writes require both --write and --confirm with the exact DEVICE path");
    }
    if (!mutating && strcmp(command, "analyze") != 0 && strcmp(command, "map") != 0) {
        usage(stderr);
        return EXIT_FAILURE;
    }

    char *journal_path = journal_arg == NULL ? default_journal_path(device_path) : xstrdup(journal_arg);
    Device dev = device_open(device_path, mutating);
    g_io.rotational = device_is_rotational(&dev);
    g_io.ram_limit = strcmp(ram_buffer_arg, "auto") == 0
                         ? automatic_ram_limit()
                         : parse_byte_size(ram_buffer_arg);
    g_io.workers = strcmp(workers_arg, "auto") == 0
                       ? automatic_worker_count(g_io.rotational)
                       : parse_size(workers_arg);
    if (g_io.workers == 0) die_msg("--workers must be at least 1");
    size_t cpus = online_cpu_count();
    if (g_io.workers > cpus * 4) die_msg("--workers is unreasonably larger than the available CPU count");
    if (mutating) install_signal_handlers();
    Fat32 fs;
    fat32_load(&fs, dev, strcmp(command, "recover") == 0);
    if (g_io.ram_limit < fs.cluster_size) g_io.ram_limit = (size_t)fs.cluster_size;

    if (mutating) print_io_configuration();

    if (strcmp(command, "recover") == 0) {
        if (!path_exists(journal_path)) die_msg("journal file does not exist");
        if (journal_has_magic(journal_path, COMPACT_JOURNAL_MAGIC)) {
            recover_compact_journal(&fs, journal_path);
        } else if (journal_has_magic(journal_path, JOURNAL_MAGIC)) {
            recover_journal(&fs, journal_path);
        } else {
            die_msg("journal has an unrecognised format");
        }
        print_io_statistics();
        fat32_unload(&fs);
        free(journal_path);
        return EXIT_SUCCESS;
    }

    if (path_exists(journal_path)) {
        die_msg("an unfinished journal exists; run recover before analysis or defragmentation");
    }

    DirRefList dir_refs = {0};
    bool need_dir_refs = strcmp(command, "compact") == 0 || strcmp(command, "defrag") == 0;
    FileList files = scan_files(&fs, need_dir_refs ? &dir_refs : NULL);
    initialise_live_map(&fs, &files);
    if (strcmp(command, "map") == 0) {
        print_map_json(&fs, &files, map_cells);
        filelist_free(&files);
        dirreflist_free(&dir_refs);
        fat32_unload(&fs);
        free(journal_path);
        return EXIT_SUCCESS;
    }
    print_analysis(&fs, &files);
    if (list) list_fragmented(&fs, &files);

    if (strcmp(command, "defrag") == 0) {
        size_t moved_directories = 0;
        size_t moved_files = 0;
        if (!files_only) {
            moved_directories = defrag_directories(&fs, journal_path, max_directories);
            printf("Relocated %zu fragmented director%s.\n", moved_directories,
                   moved_directories == 1 ? "y" : "ies");
        }
        filelist_free(&files);
        dirreflist_free(&dir_refs);
        files = scan_files(&fs, &dir_refs);
        if (!directories_only) {
            FileDefragStats file_stats = defrag_files(&fs, &files, &dir_refs,
                                                      journal_path, max_files,
                                                      transaction_files);
            moved_files = file_stats.files_moved;
            printf("Relocated %zu fragmented regular file%s.\n", moved_files,
                   moved_files == 1 ? "" : "s");
            printf("File transactions:       %zu (%zu clusters)\n",
                   file_stats.transactions, file_stats.clusters_moved);
        }
        filelist_free(&files);
        dirreflist_free(&dir_refs);
        files = scan_files(&fs, NULL);
        print_analysis(&fs, &files);
    } else if (strcmp(command, "compact") == 0) {
        uint32_t highest_before = 1;
        uint64_t holes_before = holes_below_high_water(&fs, &highest_before);
        printf("Highest allocated cluster: %" PRIu32 "\n", highest_before);
        printf("Free gaps below it:       %" PRIu64 " clusters\n", holes_before);
        CompactStats compact_stats = compact_volume(&fs, journal_path, max_clusters,
                                                   batch_clusters, max_transactions);
        printf("Compacted %zu allocated cluster%s toward the start of the volume.\n",
               compact_stats.clusters_moved, compact_stats.clusters_moved == 1 ? "" : "s");
        printf("Transactions completed:  %zu\n", compact_stats.transactions);
        printf("Whole objects packed:    %zu (%zu clusters)\n",
               compact_stats.whole_objects, compact_stats.whole_clusters);
        printf("Whole objects staged:    %zu (%zu clusters)\n",
               compact_stats.staged_objects, compact_stats.staged_clusters);
        printf("Ordered extent moves:    %zu (%zu clusters; %zu singleton%s)\n",
               compact_stats.extent_transactions, compact_stats.extent_clusters,
               compact_stats.singleton_transactions,
               compact_stats.singleton_transactions == 1 ? "" : "s");
        filelist_free(&files);
        dirreflist_free(&dir_refs);
        files = scan_files(&fs, NULL);
        print_analysis(&fs, &files);
        uint32_t highest_after = 1;
        uint64_t holes_after = holes_below_high_water(&fs, &highest_after);
        printf("Highest allocated cluster: %" PRIu32 "\n", highest_after);
        printf("Free gaps below it:       %" PRIu64 " clusters\n", holes_after);
        printf("Terminal free run:        %" PRIu64 " clusters\n", terminal_free_clusters(&fs));
    }

    if (mutating) print_io_statistics();

    filelist_free(&files);
    dirreflist_free(&dir_refs);
    fat32_unload(&fs);
    free(g_live_map_previous);
    g_live_map_previous = NULL;
    free(journal_path);
    return EXIT_SUCCESS;
}
