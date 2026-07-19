#!/usr/bin/python3
# Linux Defragger
# Author: Shannon Smith
# Purpose: Modular filesystem analysis, compaction and defragmentation support.
#
# Comments describe design intent and non-obvious behaviour. They are kept
# concise so that the implementation remains readable and maintainable.

from __future__ import annotations
import json, os, subprocess
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
MAPPER=ROOT/'gui'/'allocation_mapper.py'
env={**os.environ,'PYTHONPATH':str(ROOT/'gui')}
manifest=subprocess.run([str(MAPPER),'--list-backends'],text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=env)
assert manifest.returncode==0, manifest.stderr
items=json.loads(manifest.stdout)['backends']
by_id={x['id']:x for x in items}
assert by_id['ntfs']['capabilities']==3
assert by_id['exfat']['capabilities'] & 31 == 31
assert by_id['fat32']['capabilities'] & 28 == 28
bad=subprocess.run([str(MAPPER),'/dev/null','--fstype','ntfs'],text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=env)
assert bad.returncode!=0
print('modular mapper capability test passed')
