#!/usr/bin/env python3
"""Opt in to Codex-only iTerm2 integration in future interactive zsh shells."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shlex
import shutil
import stat
import sys
import tempfile


BEGIN = '# >>> codex-monitor iTerm2 integration >>>'
END = '# <<< codex-monitor iTerm2 integration <<<'


def managed_block(executable):
    command = shlex.quote(str(executable))
    # The leading newline belongs to the block, so removing it also restores
    # an original rc file that did not end with a newline.
    return ('\n' + BEGIN + '\n' +
            'if [[ -o interactive ]] && (( ! ${+functions[codex]} && ! ${+aliases[codex]} )); then\n'
            '  function codex() {\n'
            '    local _codex_monitor_original\n'
            '    _codex_monitor_original=$(whence -p codex)\n'
            '    if [[ -z $_codex_monitor_original ]]; then\n'
            '      print -u2 -- "codex-monitor: codex executable not found on PATH"\n'
            '      return 127\n'
            '    fi\n'
            '    if [[ -x ' + command + ' ]]; then\n'
            '      ' + command + ' launch --codex "$_codex_monitor_original" -- "$@"\n'
            '    else\n'
            '      "$_codex_monitor_original" "$@"\n'
            '    fi\n'
            '  }\n'
            'fi\n' + END + '\n').encode('utf-8')


def block_span(content):
    markers = {BEGIN.encode(): [], END.encode(): []}
    offset = 0
    for line in content.splitlines(keepends=True):
        text = line.rstrip(b'\r\n')
        if text in markers:
            markers[text].append((offset, offset + len(line)))
        offset += len(line)
    for marker, positions in markers.items():
        if content.count(marker) != len(positions):
            raise ValueError('Malformed codex-monitor integration marker; nothing changed')
    starts, ends = markers[BEGIN.encode()], markers[END.encode()]
    if not starts and not ends:
        return None
    if len(starts) != 1 or len(ends) != 1 or starts[0][0] >= ends[0][0]:
        raise ValueError('Missing, repeated, or unordered codex-monitor markers; nothing changed')
    start, finish = starts[0][0], ends[0][1]
    if start and content[start - 1:start] == b'\n':
        start -= 1
    return start, finish


def replacement(content, block, uninstall=False):
    span = block_span(content)
    if span is None:
        return content if uninstall else content + block
    start, finish = span
    return content[:start] + (b'' if uninstall else block) + content[finish:]


def validate_paths(shell_rc, executable, backup_dir):
    if shell_rc.is_symlink():
        raise ValueError('Refusing a symlink shell rc file')
    if shell_rc.exists() and not shell_rc.is_file():
        raise ValueError('Shell rc path must be a regular file')
    if any('.claude' in path.resolve().parts for path in (shell_rc, executable, backup_dir)):
        raise ValueError('This installer never uses a Claude directory')
    skill_roots = [Path.home() / '.agents/skills',
                   Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))) / 'skills']
    resolved_backup = backup_dir.resolve()
    for skill_root in skill_roots:
        resolved_root = skill_root.expanduser().resolve()
        if resolved_backup == resolved_root or resolved_root in resolved_backup.parents:
            raise ValueError('Shell rc backups must be outside Codex skill directories')


def write_rc(shell_rc, content, backup_dir):
    shell_rc.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    mode = 0o600
    if shell_rc.exists():
        mode = stat.S_IMODE(shell_rc.stat().st_mode)
        backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        backup = backup_dir / (shell_rc.name + '.backup-' + stamp)
        shutil.copy2(shell_rc, backup)
    descriptor, temporary = tempfile.mkstemp(prefix='.codex-monitor-rc-', dir=shell_rc.parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
            os.fchmod(stream.fileno(), mode)
        os.replace(temporary, shell_rc)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return str(backup) if backup else None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--shell-rc', default='~/.zshrc')
    parser.add_argument('--executable', default='~/.local/bin/codex-monitor')
    parser.add_argument('--backup-dir', default=None)
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--install', action='store_true', help='Apply the managed block')
    action.add_argument('--uninstall', action='store_true', help='Remove only the managed block')
    args = parser.parse_args()
    shell_rc = Path(args.shell_rc).expanduser().absolute()
    executable = Path(args.executable).expanduser().absolute()
    backup_dir = (Path(args.backup_dir).expanduser().absolute() if args.backup_dir
                  else shell_rc.parent / '.codex-monitor-backups')
    try:
        validate_paths(shell_rc, executable, backup_dir)
        original = shell_rc.read_bytes() if shell_rc.exists() else b''
        block = managed_block(executable)
        proposed = replacement(original, block, uninstall=args.uninstall)
        changed = proposed != original
        dry_run = not (args.install or args.uninstall)
        backup = write_rc(shell_rc, proposed, backup_dir) if changed and not dry_run else None
        print(json.dumps({'shell_rc': str(shell_rc), 'executable': str(executable),
                          'action': 'uninstall' if args.uninstall else 'install',
                          'dry_run': dry_run, 'changed': changed, 'backup': backup,
                          'managed_block': None if args.uninstall else block.decode('utf-8'),
                          'existing_shells_reloaded': False}, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError) as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
