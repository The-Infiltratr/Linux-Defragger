#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
python3 "$ROOT/tests/exfat/make_exfat_test.py" "$WORK/compact-base.exfat" >/dev/null
cp "$WORK/compact-base.exfat" "$WORK/compact.exfat"
python3 "$ROOT/gui/exfat_engine.py" compact "$WORK/compact.exfat" \
  --write --confirm "$WORK/compact.exfat" --journal "$WORK/compact.journal"
PYTHONPATH="$ROOT/gui" python3 "$ROOT/tests/exfat/test_tail_fill.py" \
  "$WORK/compact.exfat" "$WORK/compact-base.exfat" \
  "$WORK/rollback.exfat" "$WORK/forward.exfat"
python3 "$ROOT/tests/exfat/make_exfat_nofat_test.py" "$WORK/nofat.exfat"
python3 "$ROOT/gui/exfat_engine.py" compact "$WORK/nofat.exfat" \
  --write --confirm "$WORK/nofat.exfat" --journal "$WORK/nofat.journal"
PYTHONPATH="$ROOT/gui" python3 - "$WORK/nofat.exfat" <<'PY'
import sys
from exfat_engine import Volume,fragments
v=Volume(sys.argv[1]);e=v.parse()[0]
assert e.clusters == [6,5,4] and not e.nofat and fragments(e.clusters)==3
assert [v.read(v.coff(c),1) for c in e.clusters] == [b'A',b'B',b'C']
v.close();print('exFAT NoFatChain conversion during Compact passed')
PY
python3 "$ROOT/gui/exfat_engine.py" recover "$WORK/rollback.exfat" \
  --write --confirm "$WORK/rollback.exfat" --journal "$WORK/rollback.journal"
PYTHONPATH="$ROOT/gui" python3 - "$WORK/rollback.exfat" <<'PY'
import sys
from exfat_engine import Volume
v=Volume(sys.argv[1]);e=v.parse()[0]
assert e.clusters == [10,20,11] and not v.bit(4)
v.close();print('exFAT tail-fill rollback recovery passed')
PY
python3 "$ROOT/gui/exfat_engine.py" recover "$WORK/forward.exfat" \
  --write --confirm "$WORK/forward.exfat" --journal "$WORK/forward.journal"
PYTHONPATH="$ROOT/gui" python3 - "$WORK/forward.exfat" <<'PY'
import sys
from exfat_engine import Volume
v=Volume(sys.argv[1]);e=v.parse()[0]
assert e.clusters == [10,4,11] and not v.bit(20)
assert [v.read(v.coff(c),1) for c in e.clusters] == [b'A',b'B',b'C']
v.close();print('exFAT tail-fill forward recovery passed')
PY
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
python3 "$ROOT/tests/exfat/make_exfat_test.py" "$WORK/growth.exfat" >/dev/null
python3 "$ROOT/gui/exfat_engine.py" growth-defrag "$WORK/growth.exfat" \
  --write --confirm "$WORK/growth.exfat" --journal "$WORK/growth.journal" --growth-percent 10
PYTHONPATH="$ROOT/gui" python3 - "$WORK/growth.exfat" <<'PY'
import sys
from exfat_engine import Volume,growth_preflight
v=Volume(sys.argv[1]);e=v.parse()[0]
assert e.clusters == [4,5,6]
ok,reason=growth_preflight(v,10);assert ok,reason
v.close();print('exFAT Growth Defrag verification passed')
PY
PYTHONPATH="$ROOT/gui" python3 "$ROOT/gui/allocation_mapper.py" "$WORK/growth.exfat" \
  --fstype exfat --cells 64 >"$WORK/map.json"
python3 - "$WORK/map.json" <<'PY'
import json,sys
j=json.load(open(sys.argv[1]));assert j['growth_10_satisfied'] is True
assert j['capabilities'] & (1<<6)
print('exFAT Growth Defrag capability and cached preflight passed')
PY
