#!/usr/bin/env python3
"""Locate the separately installed CLI; never install implicitly."""
import json
import os
from pathlib import Path
import shutil
import sys

candidates = [os.environ.get('CODEX_MONITOR_BIN')]
location = Path(__file__).with_name('install-location.json')
if location.is_file():
    candidates.append(json.loads(location.read_text()).get('executable'))
candidates.extend([shutil.which('codex-monitor'), str(Path.home() / '.local/bin/codex-monitor')])
for candidate in candidates:
    if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
        os.execv(candidate, [candidate] + sys.argv[1:])
print('codex-monitor is not installed. Install the service repository with python3 scripts/install.py, or set CODEX_MONITOR_BIN.', file=sys.stderr)
raise SystemExit(1)
