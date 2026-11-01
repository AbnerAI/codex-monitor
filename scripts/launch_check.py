#!/usr/bin/env python3
"""Opt-in API lifecycle check confined to the current authorized iTerm2 shell.

Temporarily removes only this project's counter component, verifies automatic
addition and exact launch binding, then restores the original current-session
layout and manual attachment. Never opens another window or Codex conversation.
"""
import argparse
import copy
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import time
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from codex_monitor.daemon import control
from codex_monitor.iterm import (action, attach, attachment_path,
                                  label_for_rows, session_id)
from codex_monitor.launch import COMPONENT, EXPRESSION, Decoration, apply_profile, profile_api
from codex_monitor.rpc import CodexRPC, codex_binary, home_path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', default=os.environ.get('CODEX_MONITOR_HOME', '~/.codex-monitor'))
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    os.umask(0o077)
    root = Path(args.state_dir).expanduser().resolve()
    identifier, thread, home = session_id(), os.environ['CODEX_THREAD_ID'], home_path()
    report = {'passed': False, 'scope': 'current authorized shell only'}
    descriptor = None
    original = decoration = old_attachment = test_layout = None
    owned = []
    baseline_applied = False
    markers = {key: os.environ.get(key) for key in ('CODEX_MONITOR_LAUNCH_TOKEN', 'CODEX_MONITOR_ITERM_SESSION')}

    def observe(count):
        expected = 'monitor-' + str(count) if count else ''
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if action('probe', identifier).get('monitor') == expected:
                return expected
            time.sleep(0.2)
        raise RuntimeError('Counter did not become ' + repr(expected))

    try:
        metadata = action('probe', identifier)
        original = profile_api(identifier, 'get')  # No mutations if the API is disabled.
        path = attachment_path(root, identifier)
        if path.exists():
            old_attachment = json.loads(path.read_text())
            if old_attachment.get('launch_token'):
                raise ValueError('Do not nest this test inside an automatic launch')
        rpc = CodexRPC(home, codex_binary())
        try:
            assert rpc.target(thread)['id'] == thread
        finally:
            rpc.close()
        service = control(root, {'method': 'status'})
        baseline, _ = label_for_rows(control(root, {'method': 'list'}), thread, home)
        descriptor = os.open(metadata['tty'], os.O_WRONLY | os.O_NOCTTY)
        again = action('probe', identifier)
        if again['tty'] != os.ttyname(descriptor) or again['tty'] != metadata['tty']:
            raise ValueError('The current terminal changed during verification')
        temporary = copy.deepcopy(original)
        if temporary.get('layout'):
            temporary['layout']['components'] = [component for component in temporary['layout'].get('components', [])
                if not (component.get('class') == COMPONENT['class'] and
                        component.get('configuration', {}).get('knobs', {}).get('expression') == EXPRESSION)]
        baseline_applied = True
        apply_profile(identifier, temporary)
        decoration = Decoration(root, identifier, descriptor)
        decoration.prepare()
        test_layout = decoration.applied
        report['component_added_automatically'] = any(
            component.get('class') == COMPONENT['class'] and
            component.get('configuration', {}).get('knobs', {}).get('expression') == EXPRESSION
            for component in profile_api(identifier, 'get')['layout']['components'])
        os.environ['CODEX_MONITOR_LAUNCH_TOKEN'] = decoration.token
        os.environ['CODEX_MONITOR_ITERM_SESSION'] = identifier
        with tempfile.TemporaryDirectory(prefix='cm-auto-launch-check-') as directory:
            for index in range(2):
                inbox = Path(directory) / (str(index) + '.ndjson')
                inbox.touch()
                completed = subprocess.run([sys.executable, '-B', '-m', 'codex_monitor',
                    '--state-dir', str(root), 'watch', '--file', str(inbox),
                    '--thread', thread, '--codex-home', home, '--interval', '0.2',
                    '--name', 'codex-monitor-auto-launch-selftest'],
                    cwd=str(Path(__file__).resolve().parent.parent),
                    capture_output=True, text=True, timeout=35, check=True)
                registration = json.loads(completed.stdout)
                owned.append(registration['id'])
                if not registration.get('iterm', {}).get('attached'):
                    raise RuntimeError('The real watch command did not attach its indicator')
            actual = json.loads(attachment_path(root, identifier).read_text())
            report['exact_thread_bound'] = actual['thread_id'] == thread and actual['codex_home'] == home
            report['real_watch_command_auto_binding'] = True
            report['two_watchers_label'] = observe(baseline + 2)
            for watcher in owned:
                control(root, {'method': 'unwatch', 'id': watcher})
            owned.clear()
            observe(baseline)
        decoration.close()
        decoration = None
        report['launch_layout_restored'] = profile_api(identifier, 'get') == temporary
        report['counter_cleared_on_exit'] = action('probe', identifier).get('monitor') == ''
        report['service_instance_unchanged'] = control(root, {'method': 'status'})['instance'] == service['instance']
        report['passed'] = all(report.get(key) for key in (
            'component_added_automatically', 'exact_thread_bound', 'launch_layout_restored',
            'counter_cleared_on_exit', 'service_instance_unchanged', 'real_watch_command_auto_binding'))
    except Exception as exc:
        report['error'] = str(exc)
    finally:
        for watcher in owned:
            try:
                control(root, {'method': 'unwatch', 'id': watcher})
            except Exception as exc:
                report.setdefault('watcher_cleanup_errors', []).append(str(exc))
        if decoration is not None:
            try:
                decoration.close()
            except Exception as exc:
                report['cleanup_error'] = str(exc)
        for key, value in markers.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        if baseline_applied:
            try:
                current = profile_api(identifier, 'get')
                if current in (temporary, test_layout, original):
                    apply_profile(identifier, original)
                report['original_user_layout_restored'] = profile_api(identifier, 'get') == original
                if old_attachment and old_attachment.get('active') and not attachment_path(root, identifier).exists():
                    attach(root, SimpleNamespace(session=identifier, thread=old_attachment['thread_id'],
                                                  codex_home=old_attachment['codex_home']))
            except Exception as exc:
                report['restoration_error'] = str(exc)
            report['passed'] = bool(report['passed'] and report.get('original_user_layout_restored')
                                    and not report.get('cleanup_error') and not report.get('restoration_error')
                                    and not report.get('watcher_cleanup_errors'))
        if descriptor is not None:
            os.close(descriptor)
        Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
