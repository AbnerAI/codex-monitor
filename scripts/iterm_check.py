#!/usr/bin/env python3
"""Opt-in live check of the authorized iTerm2 session's monitor variable.

Only creates and stops its own two empty-file watchers. No notifications,
terminal input, title changes, profile edits, or service restarts are needed.
Run `codex-monitor iterm attach` in the intended conversation first.
"""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from codex_monitor.daemon import control
from codex_monitor.iterm import action, attachment_path, label_for_rows, session_id
from codex_monitor.rpc import codex_binary, home_path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', default=os.environ.get('CODEX_MONITOR_HOME', '~/.codex-monitor'))
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    os.umask(0o077)
    root = Path(args.state_dir).expanduser().resolve()
    identifier = session_id()
    thread, home = os.environ.get('CODEX_THREAD_ID'), home_path()
    if not thread:
        raise ValueError('CODEX_THREAD_ID is required')
    attachment = json.loads(attachment_path(root, identifier).read_text())
    if (not attachment.get('active') or attachment['thread_id'] != thread
            or attachment['codex_home'] != home):
        raise ValueError('Attach this exact conversation and profile first')
    before = action('probe', identifier)
    service = control(root, {'method': 'status'})
    baseline, _ = label_for_rows(control(root, {'method': 'list'}), thread, home)
    report = {'passed': False, 'session_id': identifier, 'tty': before['tty'],
              'thread_id': thread, 'baseline': baseline, 'observations': [],
              'created_watchers': [], 'stopped_watchers': []}
    owned = []

    def observe(expected):
        deadline = time.monotonic() + 10
        label = (str(expected) + (' monitor' if expected == 1 else ' monitors')) if expected else ''
        while time.monotonic() < deadline:
            metadata = action('probe', identifier)
            if metadata['title_format'] != before['title_format']:
                raise RuntimeError('Title format changed during check')
            if metadata.get('monitor') == label:
                report['observations'].append({'active': expected, 'label': label,
                                                'title_format_unchanged': True})
                return
            time.sleep(0.2)
        raise RuntimeError('Timed out waiting for ' + repr(label))

    try:
        observe(baseline)
        with tempfile.TemporaryDirectory(prefix='codex-monitor-iterm-check-') as directory:
            for index in range(2):
                inbox = Path(directory) / ('empty-' + str(index) + '.ndjson')
                inbox.touch()
                watcher = control(root, {'method': 'watch', 'spec': {
                    'source': 'file', 'path': str(inbox), 'thread_id': thread,
                    'codex_home': home, 'codex_bin': codex_binary(), 'interval': 0.2,
                    'name': 'codex-monitor-iterm-selftest'}})
                owned.append(watcher['id'])
                report['created_watchers'].append(watcher['id'])
            observe(baseline + 2)
            control(root, {'method': 'unwatch', 'id': owned[0]})
            report['stopped_watchers'].append(owned.pop(0))
            observe(baseline + 1)
            control(root, {'method': 'unwatch', 'id': owned[0]})
            report['stopped_watchers'].append(owned.pop(0))
            observe(baseline)
        after_service = control(root, {'method': 'status'})
        report['service_instance_unchanged'] = service['instance'] == after_service['instance']
        report['passed'] = report['service_instance_unchanged']
    except Exception as exc:
        report['error'] = str(exc)
    finally:
        for watcher in owned:
            control(root, {'method': 'unwatch', 'id': watcher})
            report['stopped_watchers'].append(watcher)
        Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
