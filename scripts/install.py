#!/usr/bin/env python3
"""Offline source installer: service, executable and optional Codex skill only."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


def install(source, target, backup_dir=None):
    if target.is_symlink():
        raise ValueError('Refusing to replace a symlink: ' + str(target))
    backup = None
    if target.exists():
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        name = target.name + '.backup-' + stamp
        if backup_dir is None:
            backup = target.with_name(name)
        else:
            backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            backup = backup_dir / name
        target.rename(backup)
    try:
        shutil.copytree(source, target, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    except Exception:
        # copytree may have already written part of the replacement. Remove
        # that incomplete copy before putting the original directory back.
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        elif target.exists() or target.is_symlink():
            target.unlink()
        if backup:
            backup.rename(target)
        raise
    return str(backup) if backup else None


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--prefix', default='~/.local/share/codex-monitor')
    ap.add_argument('--bin-dir', default='~/.local/bin')
    ap.add_argument('--skill-dir', default='~/.agents/skills/codex-monitor')
    ap.add_argument('--no-skill', action='store_true')
    ap.add_argument('--start', action='store_true', help='Start the installed monitor service after installation')
    a = ap.parse_args()
    root = Path(__file__).resolve().parent.parent
    prefix = Path(a.prefix).expanduser().absolute()
    bindir = Path(a.bin_dir).expanduser().absolute()
    skill = Path(a.skill_dir).expanduser().absolute()
    if any('.claude' in p.resolve().parts for p in (prefix, bindir, skill)):
        raise ValueError('This installer never writes to a Claude directory')
    prefix.mkdir(parents=True, exist_ok=True)
    backups = [install(root / 'codex_monitor', prefix / 'codex_monitor')]
    bindir.mkdir(parents=True, exist_ok=True)
    executable = bindir / 'codex-monitor'
    if executable.is_symlink():
        raise ValueError('Refusing to replace a symlink: ' + str(executable))
    if executable.exists():
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        saved = executable.with_name(executable.name + '.backup-' + stamp)
        shutil.copy2(executable, saved)
        backups.append(str(saved))
    executable.write_text('#!' + sys.executable + '\nimport sys\nsys.path.insert(0, ' + repr(str(prefix)) + ')\nfrom codex_monitor.cli import main\nmain()\n')
    executable.chmod(0o755)
    if not a.no_skill:
        skill.parent.mkdir(parents=True, exist_ok=True)
        # Codex discovers sibling skill directories, including backup copies.
        # Keep these backups with the service instead of creating duplicate
        # skills under ~/.agents/skills.
        backups.append(install(root / 'skills/codex-monitor', skill,
                               backup_dir=prefix / 'backups/skills'))
        (skill / 'scripts/install-location.json').write_text(json.dumps({'executable': str(executable)}) + '\n')
    service = None
    if a.start:
        started = subprocess.run([str(executable), 'start'], capture_output=True, text=True, check=True)
        service = json.loads(started.stdout)
    print(json.dumps({'installed': True, 'executable': str(executable), 'prefix': str(prefix),
                      'skill': None if a.no_skill else str(skill),
                      'backups': [b for b in backups if b],
                      'service_started': bool(service), 'service': service,
                      'codex_config_changed': False}, indent=2))


if __name__ == '__main__':
    main()
