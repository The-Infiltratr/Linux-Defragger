#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
BIN=${1:-$ROOT/build/linux-defragger-engine}
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

make_img() { "$ROOT/tests/make_fragmented_image.py" "$1" >/dev/null; }
expect_fail() {
  local pattern=$1; shift
  if "$@" >"$TMP/out" 2>"$TMP/err"; then
    echo "expected failure but command succeeded: $*" >&2; exit 1
  fi
  grep -q "$pattern" "$TMP/err"
}

make_img "$TMP/basic.img"
"$BIN" analyze "$TMP/basic.img" --list >/dev/null
"$BIN" map "$TMP/basic.img" --cells 64 >"$TMP/basic-map.json"
"$ROOT/tests/verify_map_json.py" "$TMP/basic-map.json" 64 >/dev/null
"$BIN" defrag "$TMP/basic.img" --write --confirm "$TMP/basic.img" \
  --max-files 1 --transaction-files 1 >/dev/null
"$ROOT/tests/verify_defragged_image.py" "$TMP/basic.img" >/dev/null

make_img "$TMP/buffered.img"
"$BIN" defrag "$TMP/buffered.img" --write --confirm "$TMP/buffered.img" \
  --max-files 1 --ram-buffer 64K --workers 2 >"$TMP/buffered.out"
grep -q 'RAM I/O buffer:' "$TMP/buffered.out"
grep -q 'Source read workers:     2' "$TMP/buffered.out"
grep -q 'Buffered data read:' "$TMP/buffered.out"
grep -q 'Buffered data written:' "$TMP/buffered.out"
"$ROOT/tests/verify_defragged_image.py" "$TMP/buffered.img" >/dev/null

make_img "$TMP/cross.img"
"$ROOT/tests/mutate_image.py" "$TMP/cross.img" crosslink
expect_fail 'cross-linked cluster' "$BIN" analyze "$TMP/cross.img"

make_img "$TMP/orphan.img"
"$ROOT/tests/mutate_image.py" "$TMP/orphan.img" orphan
expect_fail 'allocated but unreferenced cluster' "$BIN" analyze "$TMP/orphan.img"

make_img "$TMP/dirty.img"
"$ROOT/tests/mutate_image.py" "$TMP/dirty.img" dirty
expect_fail 'clean-shutdown bit is clear' "$BIN" analyze "$TMP/dirty.img"

make_img "$TMP/active.img"
"$ROOT/tests/mutate_image.py" "$TMP/active.img" active-fat1
"$BIN" analyze "$TMP/active.img" >/dev/null
"$BIN" defrag "$TMP/active.img" --write --confirm "$TMP/active.img" --max-files 1 >/dev/null
"$ROOT/tests/verify_defragged_image.py" "$TMP/active.img" 1 >/dev/null

make_img "$TMP/rollback.img"
"$ROOT/tests/mutate_image.py" "$TMP/rollback.img" interrupt-rollback --journal "$TMP/rollback.journal" --device-path "$TMP/rollback.img"
"$BIN" recover "$TMP/rollback.img" --write --confirm "$TMP/rollback.img" --journal "$TMP/rollback.journal" >/dev/null
"$ROOT/tests/verify_fragmented_image.py" "$TMP/rollback.img" >/dev/null

touch "$TMP/dummy"
make_img "$TMP/commit.img"
"$ROOT/tests/mutate_image.py" "$TMP/commit.img" interrupt-commit --journal "$TMP/commit.journal" --device-path "$TMP/commit.img"
"$BIN" recover "$TMP/commit.img" --write --confirm "$TMP/commit.img" --journal "$TMP/commit.journal" >/dev/null
"$ROOT/tests/verify_defragged_image.py" "$TMP/commit.img" >/dev/null

"$ROOT/tests/make_interleaved_compact_image.py" "$TMP/batch-recover.img" >/dev/null
"$ROOT/tests/make_batched_defrag_journal.py" "$TMP/batch-recover.img" \
  "$TMP/batch-recover.journal"
"$BIN" recover "$TMP/batch-recover.img" --write \
  --confirm "$TMP/batch-recover.img" --journal "$TMP/batch-recover.journal" >/dev/null
"$ROOT/tests/verify_batched_defrag.py" "$TMP/batch-recover.img" \
  --expect-contiguous 2 >/dev/null

make_img "$TMP/compact-basic.img"
"$BIN" compact "$TMP/compact-basic.img" --write --confirm "$TMP/compact-basic.img" \
  --batch-clusters 2 >/dev/null
"$BIN" analyze "$TMP/compact-basic.img" >/dev/null

"$ROOT/tests/make_compact_tree_image.py" "$TMP/compact-tree.img" >/dev/null
"$BIN" compact "$TMP/compact-tree.img" --write --confirm "$TMP/compact-tree.img" \
  --batch-clusters 3 >/dev/null
"$ROOT/tests/verify_compacted_tree.py" "$TMP/compact-tree.img" >/dev/null

"$ROOT/tests/make_compact_tree_image.py" "$TMP/compact-recover-prepared.img" >/dev/null
"$ROOT/tests/make_compact_journal.py" "$TMP/compact-recover-prepared.img" \
  "$TMP/compact-recover-prepared.journal"
"$BIN" recover "$TMP/compact-recover-prepared.img" --write \
  --confirm "$TMP/compact-recover-prepared.img" \
  --journal "$TMP/compact-recover-prepared.journal" >/dev/null
"$ROOT/tests/verify_compacted_tree.py" "$TMP/compact-recover-prepared.img" >/dev/null

"$ROOT/tests/make_compact_tree_image.py" "$TMP/compact-recover-partial.img" >/dev/null
"$ROOT/tests/make_compact_journal.py" "$TMP/compact-recover-partial.img" \
  "$TMP/compact-recover-partial.journal" --partial-free
"$BIN" recover "$TMP/compact-recover-partial.img" --write \
  --confirm "$TMP/compact-recover-partial.img" \
  --journal "$TMP/compact-recover-partial.journal" >/dev/null
"$ROOT/tests/verify_compacted_tree.py" "$TMP/compact-recover-partial.img" >/dev/null

"$ROOT/tests/make_fragmented_directory_image.py" "$TMP/directory-defrag.img" >/dev/null
"$BIN" analyze "$TMP/directory-defrag.img" --list >"$TMP/directory-before.txt"
grep -q 'Fragmented directories: 1 (root fragments: 2)' "$TMP/directory-before.txt"
"$BIN" defrag "$TMP/directory-defrag.img" --write --confirm "$TMP/directory-defrag.img" >/dev/null
"$ROOT/tests/verify_directory_defrag.py" "$TMP/directory-defrag.img" >/dev/null
"$BIN" analyze "$TMP/directory-defrag.img" >"$TMP/directory-after.txt"
grep -q 'Fragmented files:       0' "$TMP/directory-after.txt"
grep -q 'Fragmented directories: 0 (root fragments: 1)' "$TMP/directory-after.txt"

"$ROOT/tests/make_fragmented_directory_image.py" "$TMP/directory-defrag-16k.img" 32 >/dev/null
"$BIN" defrag "$TMP/directory-defrag-16k.img" --write \
  --confirm "$TMP/directory-defrag-16k.img" >/dev/null
"$ROOT/tests/verify_directory_defrag.py" "$TMP/directory-defrag-16k.img" >/dev/null
"$BIN" analyze "$TMP/directory-defrag-16k.img" >"$TMP/directory-16k-after.txt"
grep -q 'Sectors per cluster:    32' "$TMP/directory-16k-after.txt"
grep -q 'Fragmented directories: 0 (root fragments: 1)' "$TMP/directory-16k-after.txt"


"$ROOT/tests/make_interleaved_compact_image.py" "$TMP/batched-defrag.img" >/dev/null
"$BIN" defrag "$TMP/batched-defrag.img" --write \
  --confirm "$TMP/batched-defrag.img" --files-only \
  --transaction-files 8 --ram-buffer 64M --workers 4 \
  >"$TMP/batched-defrag.txt" 2>"$TMP/batched-defrag.err"
grep -q 'File transactions:       4 (512 clusters)' "$TMP/batched-defrag.txt"
grep -q 'Buffered data written:   8.0 MiB in 4 extents' "$TMP/batched-defrag.txt"
test "$(grep -c 'Directory metadata:      8 entries in 1 sector write' \
  "$TMP/batched-defrag.err")" -eq 4
"$ROOT/tests/verify_batched_defrag.py" "$TMP/batched-defrag.img" \
  --expect-contiguous 32 >/dev/null

"$ROOT/tests/make_interleaved_compact_image.py" "$TMP/batched-recover.img" >/dev/null
"$ROOT/tests/make_batched_defrag_journal.py" "$TMP/batched-recover.img" \
  "$TMP/batched-recover.journal"
"$BIN" recover "$TMP/batched-recover.img" --write \
  --confirm "$TMP/batched-recover.img" --journal "$TMP/batched-recover.journal" \
  --ram-buffer 64M --workers 4 >"$TMP/batched-recover.txt"
grep -q 'Buffered data written:   0.5 MiB in 1 extent' "$TMP/batched-recover.txt"
"$ROOT/tests/verify_batched_defrag.py" "$TMP/batched-recover.img" \
  --expect-contiguous 2 >/dev/null

"$ROOT/tests/make_interleaved_compact_image.py" "$TMP/interleaved-compact.img" >/dev/null
"$BIN" analyze "$TMP/interleaved-compact.img" >"$TMP/interleaved-before.txt"
grep -q 'Fragmented files:       32' "$TMP/interleaved-before.txt"
"$BIN" compact "$TMP/interleaved-compact.img" --write \
  --confirm "$TMP/interleaved-compact.img" >"$TMP/interleaved-compact.txt"
grep -q 'Whole objects packed:    32 (512 clusters)' "$TMP/interleaved-compact.txt"
grep -q 'Ordered extent moves:    0 (0 clusters; 0 singletons)' "$TMP/interleaved-compact.txt"
"$ROOT/tests/verify_interleaved_compact.py" "$TMP/interleaved-compact.img" >/dev/null
"$BIN" analyze "$TMP/interleaved-compact.img" >"$TMP/interleaved-after.txt"
grep -q 'Fragmented files:       0' "$TMP/interleaved-after.txt"
grep -q 'Free gaps below it:       0 clusters' <(cat "$TMP/interleaved-compact.txt")

"$ROOT/tests/make_interleaved_compact_image.py" "$TMP/batched-defrag.img" >/dev/null
"$BIN" defrag "$TMP/batched-defrag.img" --write \
  --confirm "$TMP/batched-defrag.img" --files-only \
  --transaction-files 8 --ram-buffer 64M --workers 4 >"$TMP/batched-defrag.txt"
grep -q 'File transactions:       4 (512 clusters)' "$TMP/batched-defrag.txt"
grep -q 'Buffered data written:   8.0 MiB in 4 extents' "$TMP/batched-defrag.txt"
"$ROOT/tests/verify_batched_defrag.py" "$TMP/batched-defrag.img" \
  --expect-contiguous 32 >/dev/null

"$ROOT/tests/make_interleaved_compact_image.py" "$TMP/interrupt.img" >/dev/null
"$BIN" defrag "$TMP/interrupt.img" --write --confirm "$TMP/interrupt.img" \
  --files-only --ram-buffer 64K --workers 1 --journal "$TMP/interrupt.journal" \
  >"$TMP/interrupt.out" 2>"$TMP/interrupt.err" &
interrupt_pid=$!
sleep 0.02
kill -INT "$interrupt_pid" 2>/dev/null || true
wait "$interrupt_pid"
test ! -e "$TMP/interrupt.journal"
"$BIN" analyze "$TMP/interrupt.img" >/dev/null

"$ROOT/tests/make_gapped_contiguous_image.py" "$TMP/gapped-contiguous.img" >/dev/null
"$BIN" analyze "$TMP/gapped-contiguous.img" >"$TMP/gapped-before.txt"
grep -q 'Fragmented files:       0' "$TMP/gapped-before.txt"
"$BIN" compact "$TMP/gapped-contiguous.img" --write \
  --confirm "$TMP/gapped-contiguous.img" >"$TMP/gapped-compact.txt"
grep -q 'Whole objects staged:    1 (8 clusters)' "$TMP/gapped-compact.txt"
grep -q 'Ordered extent moves:    0 (0 clusters; 0 singletons)' "$TMP/gapped-compact.txt"
"$ROOT/tests/verify_gapped_contiguous.py" "$TMP/gapped-contiguous.img" >/dev/null
"$BIN" analyze "$TMP/gapped-contiguous.img" >"$TMP/gapped-after.txt"
grep -q 'Fragmented files:       0' "$TMP/gapped-after.txt"
grep -q 'Free gaps below it:       0 clusters' "$TMP/gapped-compact.txt"

echo 'all tests passed'
