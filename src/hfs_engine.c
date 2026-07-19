/*
 * Linux Defragger - classic HFS relocation engine
 * Author: Shannon Smith
 *
 * This engine uses the bundled libhfs implementation to update the volume
 * bitmap, catalogue record and extents-overflow B-tree.  Each fork move is
 * protected by an external phase journal; the old allocation remains intact
 * until the catalogue record durably points at the destination.
 */

#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>

#include "libhfs.h"
#include "file.h"
#include "volume.h"
#include "btree.h"
#include "block.h"

#define JOURNAL_MAGIC "LDFHFS1"
#define JOURNAL_VERSION 1U
#define MAX_EXTENTS 512U
#define MAX_PATH_LEN 2048U

static volatile sig_atomic_t stop_requested = 0;

static void on_sigint(int sig) {
    (void)sig;
    stop_requested = 1;
}

typedef struct {
    uint32_t start;
    uint32_t count;
} move_extent;

typedef struct {
    char magic[8];
    uint32_t version;
    uint32_t stage;
    uint32_t create_date;
    uint32_t allocation_block_size;
    uint32_t fork;
    uint32_t extent_count;
    uint32_t destination_start;
    uint32_t destination_count;
    uint32_t path_length;
} journal_header;

typedef struct {
    char path[MAX_PATH_LEN];
    int fork;
    unsigned long logical_length;
    unsigned long physical_length;
    move_extent extents[MAX_EXTENTS];
    unsigned int extent_count;
    unsigned int fragments;
    unsigned int total_blocks;
    unsigned int first_block;
} fork_info;

typedef struct {
    fork_info *items;
    size_t count;
    size_t capacity;
    unsigned long files;
    unsigned long directories;
} fork_list;

static int fsync_volume(hfsvol *vol) {
    if (hfs_flush(vol) == -1) {
        fprintf(stderr, "hfs-engine: hfs_flush: %s\n", hfs_error ? hfs_error : "unknown error");
        return -1;
    }
    if (fsync((int)(intptr_t)vol->priv) == -1) {
        perror("hfs-engine: fsync");
        return -1;
    }
    return 0;
}

static int journal_sync_dir(const char *path) {
    char copy[PATH_MAX];
    char *slash;
    int fd;
    if (strlen(path) >= sizeof(copy)) return -1;
    strcpy(copy, path);
    slash = strrchr(copy, '/');
    if (slash) {
        if (slash == copy) slash[1] = '\0'; else *slash = '\0';
    } else {
        strcpy(copy, ".");
    }
    fd = open(copy, O_RDONLY | O_DIRECTORY);
    if (fd == -1) return -1;
    (void)fsync(fd);
    close(fd);
    return 0;
}

static int write_journal(const char *path, const journal_header *header,
                         const move_extent *extents, const char *file_path) {
    char tmp[PATH_MAX];
    int fd;
    ssize_t n;
    if (snprintf(tmp, sizeof(tmp), "%s.tmp", path) >= (int)sizeof(tmp)) return -1;
    fd = open(tmp, O_WRONLY | O_CREAT | O_TRUNC, 0600);
    if (fd == -1) return -1;
#define WRITE_ALL(ptr,len) do { \
    const unsigned char *p_ = (const unsigned char *)(ptr); size_t l_ = (len); \
    while (l_) { n = write(fd, p_, l_); if (n <= 0) { close(fd); unlink(tmp); return -1; } p_ += n; l_ -= (size_t)n; } \
} while (0)
    WRITE_ALL(header, sizeof(*header));
    WRITE_ALL(extents, header->extent_count * sizeof(*extents));
    WRITE_ALL(file_path, header->path_length);
#undef WRITE_ALL
    if (fsync(fd) == -1) { close(fd); unlink(tmp); return -1; }
    if (close(fd) == -1) { unlink(tmp); return -1; }
    if (rename(tmp, path) == -1) { unlink(tmp); return -1; }
    journal_sync_dir(path);
    return 0;
}

static int read_journal(const char *path, journal_header *header,
                        move_extent *extents, char *file_path) {
    int fd = open(path, O_RDONLY);
    ssize_t n;
    size_t left;
    unsigned char *p;
    if (fd == -1) return -1;
#define READ_ALL(ptr,len) do { p = (unsigned char *)(ptr); left = (len); while (left) { n = read(fd,p,left); if (n <= 0) { close(fd); return -1; } p += n; left -= (size_t)n; } } while (0)
    READ_ALL(header, sizeof(*header));
    if (memcmp(header->magic, JOURNAL_MAGIC, 7) || header->version != JOURNAL_VERSION ||
        header->extent_count > MAX_EXTENTS || header->path_length >= MAX_PATH_LEN) {
        close(fd); errno = EINVAL; return -1;
    }
    READ_ALL(extents, header->extent_count * sizeof(*extents));
    READ_ALL(file_path, header->path_length);
#undef READ_ALL
    file_path[header->path_length] = '\0';
    close(fd);
    return 0;
}

static int list_append(fork_list *list, const fork_info *info) {
    if (list->count == list->capacity) {
        size_t next = list->capacity ? list->capacity * 2 : 64;
        fork_info *items = realloc(list->items, next * sizeof(*items));
        if (!items) return -1;
        list->items = items;
        list->capacity = next;
    }
    list->items[list->count++] = *info;
    return 0;
}

static int collect_fork(hfsfile *file, int fork, fork_info *info) {
    ExtDataRec current;
    ExtDataRec *inline_extents;
    unsigned long *logical_length, *physical_length;
    unsigned int logical_block = 0;
    unsigned int total_blocks;
    unsigned int i;
    node n;

    if (hfs_setfork(file, fork) == -1) return -1;
    f_getptrs(file, &inline_extents, &logical_length, &physical_length);
    total_blocks = (unsigned int)(*physical_length / file->vol->mdb.drAlBlkSiz);
    info->fork = fork;
    info->logical_length = *logical_length;
    info->physical_length = *physical_length;
    info->extent_count = 0;
    info->fragments = 0;
    info->total_blocks = total_blocks;
    info->first_block = UINT_MAX;
    memcpy(&current, inline_extents, sizeof(current));

    while (logical_block < total_blocks) {
        for (i = 0; i < 3 && logical_block < total_blocks; ++i) {
            unsigned int count = current[i].xdrNumABlks;
            unsigned int start = current[i].xdrStABN;
            if (!count) return -1;
            if (info->extent_count >= MAX_EXTENTS) return -1;
            info->extents[info->extent_count].start = start;
            info->extents[info->extent_count].count = count;
            if (info->extent_count == 0) {
                info->first_block = start;
                info->fragments = 1;
            } else {
                move_extent *previous = &info->extents[info->extent_count - 1];
                if (previous->start + previous->count != start) info->fragments++;
            }
            info->extent_count++;
            logical_block += count;
        }
        if (logical_block < total_blocks) {
            memset(&n, 0, sizeof(n));
            if (v_extsearch(file, logical_block, &current, &n) <= 0) return -1;
        }
    }
    return 0;
}

static int scan_directory(hfsvol *vol, const char *path, fork_list *list) {
    hfsdir *dir;
    hfsdirent ent;
    char child[MAX_PATH_LEN];
    dir = hfs_opendir(vol, path);
    if (!dir) return -1;
    if (!strcmp(path, ":")) list->directories = 1;
    while (hfs_readdir(dir, &ent) == 0) {
        hfsfile *file;
        fork_info info;
        if (!strcmp(ent.name, ".") || !strcmp(ent.name, "..")) continue;
        if (!strcmp(path, ":")) {
            if (snprintf(child, sizeof(child), ":%s", ent.name) >= (int)sizeof(child)) continue;
        } else {
            if (snprintf(child, sizeof(child), "%s:%s", path, ent.name) >= (int)sizeof(child)) continue;
        }
        if (ent.flags & HFS_ISDIR) {
            list->directories++;
            if (scan_directory(vol, child, list) == -1) { hfs_closedir(dir); return -1; }
            continue;
        }
        list->files++;
        file = hfs_open(vol, child);
        if (!file) { hfs_closedir(dir); return -1; }
        memset(&info, 0, sizeof(info));
        strncpy(info.path, child, sizeof(info.path) - 1);
        if (collect_fork(file, 0, &info) == 0 && info.total_blocks && list_append(list, &info) == -1) {
            hfs_close(file); hfs_closedir(dir); return -1;
        }
        memset(&info, 0, sizeof(info));
        strncpy(info.path, child, sizeof(info.path) - 1);
        if (collect_fork(file, 1, &info) == 0 && info.total_blocks && list_append(list, &info) == -1) {
            hfs_close(file); hfs_closedir(dir); return -1;
        }
        if (hfs_close(file) == -1) { hfs_closedir(dir); return -1; }
    }
    hfs_closedir(dir);
    return 0;
}

static int block_is_used(hfsvol *vol, unsigned int block) {
    return BMTST(vol->vbm, block) != 0;
}

static int set_exact_allocation(hfsvol *vol, unsigned int start, unsigned int count, int allocate) {
    unsigned int i;
    if (start + count > vol->mdb.drNmAlBlks) return -1;
    if (v_dirty(vol) == -1) return -1;
    for (i = 0; i < count; ++i) {
        int used = block_is_used(vol, start + i);
        if (allocate) {
            if (used) return -1;
        } else if (!used) {
            continue;
        }
    }
    for (i = 0; i < count; ++i) {
        if (allocate) BMSET(vol->vbm, start + i); else BMCLR(vol->vbm, start + i);
    }
    if (allocate) vol->mdb.drFreeBks -= count; else vol->mdb.drFreeBks += count;
    vol->flags |= HFS_VOL_UPDATE_MDB | HFS_VOL_UPDATE_VBM;
    return 0;
}

static int free_source_extents(hfsvol *vol, const move_extent *extents, unsigned int count) {
    unsigned int i;
    for (i = 0; i < count; ++i) {
        ExtDescriptor value;
        value.xdrStABN = extents[i].start;
        value.xdrNumABlks = extents[i].count;
        if (v_freeblocks(vol, &value) == -1) return -1;
    }
    return 0;
}

static int find_free_run(hfsvol *vol, unsigned int count, unsigned int *start_out) {
    unsigned int start, run = 0;
    for (start = 0; start < vol->mdb.drNmAlBlks; ++start) {
        if (!block_is_used(vol, start)) {
            run++;
            if (run >= count) { *start_out = start + 1 - count; return 0; }
        } else run = 0;
    }
    return -1;
}

static int copy_blocks(hfsvol *vol, const move_extent *src, unsigned int src_count,
                       unsigned int dst_start, unsigned int total) {
    block b;
    unsigned int extent_index = 0, in_extent = 0, logical = 0, logical_block;
    while (logical < total) {
        unsigned int src_ab = src[extent_index].start + in_extent;
        for (logical_block = 0; logical_block < vol->lpa; ++logical_block) {
            if (b_readab(vol, src_ab, logical_block, &b) == -1 ||
                b_writeab(vol, dst_start + logical, logical_block, &b) == -1) return -1;
        }
        logical++;
        in_extent++;
        if (in_extent == src[extent_index].count) { extent_index++; in_extent = 0; }
        if (extent_index > src_count) return -1;
    }
    return 0;
}

static int delete_overflow_records(hfsfile *file, const move_extent *old, unsigned int old_count) {
    unsigned int i, logical = 0;
    ExtDataRec dummy;
    node n;
    for (i = 0; i < old_count; ++i) {
        if (i >= 3 && (i % 3) == 0) {
            memset(&n, 0, sizeof(n));
            if (v_extsearch(file, logical, &dummy, &n) > 0) {
                if (bt_delete(&file->vol->ext, HFS_NODEREC(n, n.rnum)) == -1) return -1;
            }
        }
        logical += old[i].count;
    }
    return 0;
}

static int switch_to_destination(hfsfile *file, int fork, unsigned int dst_start,
                                 unsigned int total_blocks) {
    ExtDataRec *inline_extents;
    unsigned long *logical_length, *physical_length;
    if (hfs_setfork(file, fork) == -1) return -1;
    f_getptrs(file, &inline_extents, &logical_length, &physical_length);
    (void)logical_length;
    memset(inline_extents, 0, sizeof(*inline_extents));
    (*inline_extents)[0].xdrStABN = dst_start;
    (*inline_extents)[0].xdrNumABlks = total_blocks;
    *physical_length = (unsigned long)total_blocks * file->vol->mdb.drAlBlkSiz;
    file->flags |= HFS_FILE_UPDATE_CATREC;
    return f_flush(file);
}

static int fork_points_to(hfsfile *file, int fork, unsigned int start, unsigned int count) {
    fork_info info;
    memset(&info, 0, sizeof(info));
    if (collect_fork(file, fork, &info) == -1) return 0;
    return info.extent_count == 1 && info.extents[0].start == start && info.extents[0].count == count;
}

static int move_one(const char *device, const fork_info *info, unsigned int destination,
                    const char *journal_path) {
    hfsvol *vol = NULL;
    hfsfile *file = NULL;
    journal_header header;
    int rc = -1;
    memset(&header, 0, sizeof(header));
    memcpy(header.magic, JOURNAL_MAGIC, 7);
    header.version = JOURNAL_VERSION;
    header.stage = 0;
    header.fork = (uint32_t)info->fork;
    header.extent_count = info->extent_count;
    header.destination_start = destination;
    header.destination_count = info->total_blocks;
    header.path_length = (uint32_t)strlen(info->path);

    vol = hfs_mount(device, 0, HFS_MODE_RDWR | HFS_OPT_NOCACHE);
    if (!vol) goto done;
    header.create_date = vol->mdb.drCrDate;
    header.allocation_block_size = vol->mdb.drAlBlkSiz;
    if (write_journal(journal_path, &header, info->extents, info->path) == -1) goto done;
    if (set_exact_allocation(vol, destination, info->total_blocks, 1) == -1 || fsync_volume(vol) == -1) goto done;
    if (copy_blocks(vol, info->extents, info->extent_count, destination, info->total_blocks) == -1 || fsync_volume(vol) == -1) goto done;
    header.stage = 1;
    if (write_journal(journal_path, &header, info->extents, info->path) == -1) goto done;
    if (getenv("LINUX_DEFRAGGER_TEST_FAIL_STAGE") && !strcmp(getenv("LINUX_DEFRAGGER_TEST_FAIL_STAGE"), "copied")) { errno = EINTR; goto done; }

    file = hfs_open(vol, info->path);
    if (!file) goto done;
    if (switch_to_destination(file, info->fork, destination, info->total_blocks) == -1 || fsync_volume(vol) == -1) goto done;
    header.stage = 2;
    if (write_journal(journal_path, &header, info->extents, info->path) == -1) goto done;
    if (getenv("LINUX_DEFRAGGER_TEST_FAIL_STAGE") && !strcmp(getenv("LINUX_DEFRAGGER_TEST_FAIL_STAGE"), "switched")) { errno = EINTR; goto done; }
    if (delete_overflow_records(file, info->extents, info->extent_count) == -1 || fsync_volume(vol) == -1) goto done;
    if (free_source_extents(vol, info->extents, info->extent_count) == -1 || fsync_volume(vol) == -1) goto done;
    header.stage = 3;
    if (write_journal(journal_path, &header, info->extents, info->path) == -1) goto done;
    unlink(journal_path);
    rc = 0;

done:
    if (file) hfs_close(file);
    if (vol) hfs_umount(vol);
    if (rc == -1) fprintf(stderr, "hfs-engine: %s\n", hfs_error ? hfs_error : strerror(errno));
    return rc;
}

static int recover_move(const char *device, const char *journal_path) {
    journal_header header;
    move_extent extents[MAX_EXTENTS];
    char path[MAX_PATH_LEN];
    hfsvol *vol = NULL;
    hfsfile *file = NULL;
    int committed;
    if (read_journal(journal_path, &header, extents, path) == -1) {
        perror("hfs-engine: read journal"); return -1;
    }
    vol = hfs_mount(device, 0, HFS_MODE_RDWR | HFS_OPT_NOCACHE);
    if (!vol) goto fail;
    if (vol->mdb.drCrDate != header.create_date || vol->mdb.drAlBlkSiz != header.allocation_block_size) {
        fprintf(stderr, "hfs-engine: journal does not match volume\n"); goto fail;
    }
    file = hfs_open(vol, path);
    if (!file) goto fail;
    committed = fork_points_to(file, (int)header.fork, header.destination_start, header.destination_count);
    if (committed) {
        unsigned int i;
        for (i = 0; i < header.destination_count; ++i) {
            if (!block_is_used(vol, header.destination_start + i)) {
                if (set_exact_allocation(vol, header.destination_start + i, 1, 1) == -1) goto fail;
            }
        }
        if (delete_overflow_records(file, extents, header.extent_count) == -1) goto fail;
        if (free_source_extents(vol, extents, header.extent_count) == -1) goto fail;
        printf("Recovery completed the committed HFS destination.\n");
    } else {
        unsigned int i;
        for (i = 0; i < header.destination_count; ++i) {
            unsigned int block_num = header.destination_start + i;
            if (block_is_used(vol, block_num)) {
                ExtDescriptor one; one.xdrStABN = block_num; one.xdrNumABlks = 1;
                if (v_freeblocks(vol, &one) == -1) goto fail;
            }
        }
        printf("Recovery rolled back the uncommitted HFS destination.\n");
    }
    if (fsync_volume(vol) == -1) goto fail;
    hfs_close(file); file = NULL;
    hfs_umount(vol); vol = NULL;
    unlink(journal_path);
    return 0;
fail:
    fprintf(stderr, "hfs-engine: recovery: %s\n", hfs_error ? hfs_error : strerror(errno));
    if (file) hfs_close(file);
    if (vol) hfs_umount(vol);
    return -1;
}

static int choose_and_move(const char *device, const char *mode, const char *journal,
                           unsigned int max_files) {
    unsigned int moved = 0;
    while (!stop_requested) {
        hfsvol *vol = hfs_mount(device, 0, HFS_MODE_RDWR | HFS_OPT_NOCACHE);
        fork_list list = {0};
        fork_info *selected = NULL;
        unsigned int destination = 0;
        size_t i;
        if (!vol) { fprintf(stderr, "hfs-engine: %s\n", hfs_error); return -1; }
        if (scan_directory(vol, ":", &list) == -1) {
            fprintf(stderr, "hfs-engine: scan: %s\n", hfs_error ? hfs_error : "failed");
            hfs_umount(vol); free(list.items); return -1;
        }
        for (i = 0; i < list.count; ++i) {
            fork_info *candidate = &list.items[i];
            unsigned int run;
            if (!strcmp(mode, "defrag") && candidate->fragments <= 1) continue;
            if (find_free_run(vol, candidate->total_blocks, &run) == -1) continue;
            if (!strcmp(mode, "compact") && run >= candidate->first_block) continue;
            if (!selected || (!strcmp(mode, "defrag") && candidate->fragments > selected->fragments) ||
                (!strcmp(mode, "compact") && candidate->first_block < selected->first_block)) {
                selected = candidate; destination = run;
            }
        }
        if (!selected) {
            hfs_umount(vol); free(list.items); break;
        }
        printf("move: FILE %s [%s fork] (%u blocks, %u fragments) -> block %u\n",
               selected->path, selected->fork ? "resource" : "data",
               selected->total_blocks, selected->fragments, destination);
        fflush(stdout);
        /* Close the scan handle before the isolated journalled transaction. */
        hfs_umount(vol);
        if (move_one(device, selected, destination, journal) == -1) { free(list.items); return -1; }
        free(list.items);
        moved++;
        if (max_files && moved >= max_files) break;
    }
    printf("Relocated %u classic HFS forks.\n", moved);
    if (stop_requested) printf("Stop requested; active transaction completed safely.\n");
    return 0;
}

static int scan_json(const char *device) {
    hfsvol *vol = hfs_mount(device, 0, HFS_MODE_RDONLY | HFS_OPT_NOCACHE);
    fork_list list = {0};
    size_t i, j;
    unsigned long fragmented_files = 0;
    if (!vol) { fprintf(stderr, "hfs-engine: %s\n", hfs_error); return -1; }
    if (scan_directory(vol, ":", &list) == -1) {
        fprintf(stderr, "hfs-engine: scan: %s\n", hfs_error ? hfs_error : "failed");
        hfs_umount(vol); free(list.items); return -1;
    }
    for (i = 0; i < list.count; ++i) {
        if (list.items[i].fragments <= 1) continue;
        for (j = 0; j < i; ++j)
            if (!strcmp(list.items[j].path, list.items[i].path) && list.items[j].fragments > 1) break;
        if (j == i) fragmented_files++;
    }
    printf("{\"files\":%lu,\"directories\":%lu,\"fragmented_files\":%lu,\"fragmented_directories\":0,\"fragmented_extents\":[",
           list.files, list.directories, fragmented_files);
    {
        int first = 1;
        for (i = 0; i < list.count; ++i) {
            if (list.items[i].fragments <= 1) continue;
            for (j = 0; j < list.items[i].extent_count; ++j) {
                if (!first) putchar(','); first = 0;
                printf("[%u,%u]", list.items[i].extents[j].start, list.items[i].extents[j].count);
            }
        }
    }
    printf("]}\n");
    hfs_umount(vol); free(list.items); return 0;
}


int main(int argc, char **argv) {
    const char *operation, *device, *journal = NULL, *confirm = NULL;
    unsigned int max_files = 0;
    int write_requested = 0;
    int i;
    struct sigaction sa;
    memset(&sa, 0, sizeof(sa)); sa.sa_handler = on_sigint; sigaction(SIGINT, &sa, NULL);
    if (argc < 3) {
        fprintf(stderr, "usage: hfs-engine OPERATION DEVICE --write --confirm DEVICE --journal PATH\n");
        return 2;
    }
    operation = argv[1]; device = argv[2];
    if (!strcmp(operation, "scan-json")) return scan_json(device) == 0 ? 0 : 1;
    for (i = 3; i < argc; ++i) {
        if (!strcmp(argv[i], "--write")) write_requested = 1;
        else if (!strcmp(argv[i], "--confirm") && i + 1 < argc) confirm = argv[++i];
        else if (!strcmp(argv[i], "--journal") && i + 1 < argc) journal = argv[++i];
        else if (!strcmp(argv[i], "--max-files") && i + 1 < argc) max_files = (unsigned int)strtoul(argv[++i], NULL, 10);
        else if ((!strcmp(argv[i], "--ram-buffer") || !strcmp(argv[i], "--workers") ||
                  !strcmp(argv[i], "--transaction-files") || !strcmp(argv[i], "--live-map-cells")) && i + 1 < argc) ++i;
    }
    if (!write_requested || !confirm || strcmp(confirm, device) || !journal) {
        fprintf(stderr, "hfs-engine: write confirmation and journal are required\n"); return 2;
    }
    if (!strcmp(operation, "recover")) return recover_move(device, journal) == 0 ? 0 : 1;
    if (access(journal, F_OK) == 0) {
        fprintf(stderr, "hfs-engine: unfinished journal exists; run Recover first\n"); return 1;
    }
    if (strcmp(operation, "defrag") && strcmp(operation, "compact")) {
        fprintf(stderr, "hfs-engine: unsupported operation\n"); return 2;
    }
    return choose_and_move(device, operation, journal, max_files) == 0 ? 0 : 1;
}
