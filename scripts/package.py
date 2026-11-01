#!/usr/bin/env python3
"""Build clean source and skill archives without account/runtime files."""
from pathlib import Path
import zipfile

root = Path(__file__).resolve().parent.parent
dest = root / 'dist'
dest.mkdir(exist_ok=True)
allowed = {'codex_monitor', 'skills', 'scripts', 'tests', 'docs', 'examples', '.github',
           'README.md', 'README.zh-CN.md', 'pyproject.toml', 'LICENSE', '.gitignore'}
with zipfile.ZipFile(dest / 'codex-monitor-0.1.0.zip', 'w', zipfile.ZIP_DEFLATED) as archive:
    for p in sorted(root.rglob('*')):
        rel = p.relative_to(root)
        if (p.is_file() and rel.parts[0] in allowed and '__pycache__' not in rel.parts
                and p.suffix != '.pyc' and p.name != 'install-location.json'):
            archive.write(p, str(Path('codex-monitor') / rel))
with zipfile.ZipFile(dest / 'codex-monitor.skill', 'w', zipfile.ZIP_DEFLATED) as archive:
    for p in sorted((root / 'skills/codex-monitor').rglob('*')):
        if p.is_file() and p.name != 'install-location.json' and '__pycache__' not in p.parts:
            archive.write(p, str(p.relative_to(root / 'skills')))
print(dest / 'codex-monitor-0.1.0.zip')
print(dest / 'codex-monitor.skill')
