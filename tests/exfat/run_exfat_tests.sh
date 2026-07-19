#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
python3 "$ROOT/tests/exfat/make_exfat_test.py" "$WORK/test.exfat" >/dev/null
python3 "$ROOT/gui/exfat_engine.py" defrag "$WORK/test.exfat" \
  --write --confirm "$WORK/test.exfat" --journal "$WORK/test.journal" --max-files 1
PYTHONPATH="$ROOT/gui" python3 - "$WORK/test.exfat" <<'PY'
import sys
from exfat_engine import Volume
v=Volume(sys.argv[1]);e=v.parse()[0]
assert e.clusters == [4,5,6]
assert [v.read(v.coff(c),1) for c in e.clusters] == [b'A',b'B',b'C']
v.close()
print('exFAT relocation and payload verification passed')
PY
