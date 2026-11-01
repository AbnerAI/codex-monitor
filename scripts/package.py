#!/usr/bin/env python3
"""Build clean source and skill archives without account/runtime files."""
from pathlib import Path
import sys
import zipfile

root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root))
from codex_monitor import __version__

dest = root / 'dist'
dest.mkdir(exist_ok=True)
source_archive = dest / ('codex-monitor-' + __version__ + '.zip')
allowed = {'codex_monitor', 'skills', 'scripts', 'tests', 'docs', 'examples', '.github',
           'README.md', 'README.zh-CN.md', 'pyproject.toml', 'LICENSE', '.gitignore'}
with zipfile.ZipFile(source_archive, 'w', zipfile.ZIP_DEFLATED) as archive:
    for p in sorted(root.rglob('*')):
        rel = p.relative_to(root)
        if (p.is_file() and rel.parts[0] in allowed and '__pycache__' not in rel.parts
                and p.suffix != '.pyc' and p.name != 'install-location.json'):
            archive.write(p, str(Path('codex-monitor') / rel))
with zipfile.ZipFile(dest / 'codex-monitor.skill', 'w', zipfile.ZIP_DEFLATED) as archive:
    for p in sorted((root / 'skills/codex-monitor').rglob('*')):
        if p.is_file() and p.name != 'install-location.json' and '__pycache__' not in p.parts:
            archive.write(p, str(p.relative_to(root / 'skills')))
print(source_archive)
print(dest / 'codex-monitor.skill')
