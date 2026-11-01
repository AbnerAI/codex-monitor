#!/usr/bin/env python3
"""Observe an actual Codex conversation go idle, wake, and acknowledge a marker.

Run `arm` inside the conversation being verified. The agent must then end its
turn. The independent observer appends a marker only after seeing that exact
thread idle. On wake-up, the agent replies with the returned ack_token and reads
the report. No extra model session or credential/configuration changes are used.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from codex_monitor.daemon import control
from codex_monitor.rpc import CodexRPC, codex_binary, home_path


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def agent_texts(value):
    """Extract actual agent text, not source output, tool args, or user echoes."""
    if isinstance(value, list):
        for item in value:
            yield from agent_texts(item)
    elif isinstance(value, dict):
        if value.get('type') == 'agentMessage':
            if isinstance(value.get('text'), str):
                yield value['text']
            return
        if value.get('role') == 'assistant':
            content = value.get('content', [])
            if isinstance(content, str):
                yield content
            elif isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and item.get('type') in ('output_text', 'text'):
                        yield item.get('text', '')
            return
        # A functionCallOutput or commandExecution body can itself contain JSON
        # looking like an assistant message. Do not descend into source data.
        if value.get('type') in ('functionCallOutput', 'commandExecution', 'userMessage'):
            return
        for item in value.values():
            if isinstance(item, (dict, list)):
                yield from agent_texts(item)


def find_agent_ack(entries, turn_id, token):
    """Require the canonical app-server entry and returned turn association."""
    for entry in entries:
        if not isinstance(entry, dict) or entry.get('turnId') != turn_id:
            continue
        item = entry.get('item', {})
        if (item.get('type') == 'agentMessage' and isinstance(item.get('id'), str)
                and isinstance(item.get('text'), str) and token in item['text']):
            return entry
    return None


def save(path, value):
    temp = path.with_suffix('.writing')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    os.replace(temp, path)


def arm(args):
    os.umask(0o077)
    thread = os.environ.get('CODEX_THREAD_ID')
    if not thread:
        raise ValueError('Run from the exact Codex conversation to verify')
    state = str(Path(args.state_dir).expanduser().resolve())
    service = control(state, {'method': 'status'})
    home, binary = home_path(), codex_binary()
    rpc = CodexRPC(home, binary)
    try:
        target = rpc.target(thread)
        if target.get('status', {}).get('type') != 'active':
            raise ValueError('Arm this check during an active Codex turn')
        recent = rpc.request('thread/items/list', {
            'threadId': thread, 'limit': 3, 'sortDirection': 'desc'})
        history_types = sorted({item.get('type', '') for item in recent.get('data', [])})
    finally:
        rpc.close()
    directory = Path(tempfile.mkdtemp(prefix='codex-monitor-session-check-')).resolve()
    inbox = directory / 'inbox.ndjson'
    inbox.touch()
    marker = 'CM_IDLE_' + uuid.uuid4().hex
    registration = control(state, {'method': 'watch', 'spec': {
        'source': 'file', 'path': str(inbox), 'thread_id': thread,
        'codex_home': home, 'codex_bin': binary, 'interval': 0.2,
        'name': 'codex-monitor-session-acceptance', 'once': True}})
    plan = {'thread_id': thread, 'codex_home': home, 'codex_bin': binary,
            'state_dir': state, 'directory': str(directory), 'inbox': str(inbox),
            'watcher_id': registration['id'], 'marker': marker,
            'ack_token': 'MONITOR_ACK:' + marker, 'armed_at': timestamp(),
            'expires_at': time.time() + args.timeout, 'service_instance': service['instance'],
            'initial_thread_status': target['status'], 'history_item_types': history_types}
    save(directory / 'plan.json', plan)
    save(directory / 'report.json', {'stage': 'waiting_for_idle', 'passed': False,
                                     'thread_id': thread, 'watcher_id': registration['id']})
    try:
        with (directory / 'observer.log').open('ab') as log:
            process = subprocess.Popen(
                [sys.executable, '-B', str(Path(__file__).resolve()), 'observe', '--directory', str(directory)],
                stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
    except Exception:
        control(state, {'method': 'unwatch', 'id': registration['id']})
        raise
    print(json.dumps({'armed': True, 'directory': str(directory), 'observer_pid': process.pid,
                      'thread_id': thread, 'watcher_id': registration['id'],
                      'ack_token': plan['ack_token'], 'timeout_seconds': args.timeout,
                      'next': 'End this turn; after the marker wakes the conversation, reply with ack_token and read report.json'}, indent=2))


def observe(args):
    directory = Path(args.directory).resolve()
    plan = json.loads((directory / 'plan.json').read_text())
    report_path = directory / 'report.json'
    report = json.loads(report_path.read_text())
    rpc = CodexRPC(plan['codex_home'], plan['codex_bin'])
    idle_since = None
    try:
        while time.time() < plan['expires_at']:
            status = rpc.target(plan['thread_id'])['status']
            if status.get('type') == 'idle':
                idle_since = idle_since or time.monotonic()
                if time.monotonic() - idle_since >= 0.5:
                    break
            else:
                idle_since = None
            time.sleep(0.2)
        else:
            raise TimeoutError('The exact conversation never became idle before the deadline')
        report.update(stage='waiting_for_delivery', observed_idle_at=timestamp(),
                      observed_idle_status=status, independently_observed_idle=True)
        save(report_path, report)
        with Path(plan['inbox']).open('a') as stream:
            stream.write(json.dumps({'content': plan['marker']}) + '\n')
            stream.flush()
            os.fsync(stream.fileno())
        report['file_appended_at'] = timestamp()
        event = None
        while time.time() < plan['expires_at']:
            events = control(plan['state_dir'], {'method': 'events', 'id': plan['watcher_id']})
            if events and events[0]['status'] == 'uncertain':
                raise RuntimeError('Delivery acknowledgement is uncertain; not resubmitting')
            if events and events[0]['status'] == 'accepted':
                event = events[0]
                break
            time.sleep(0.2)
        if not event:
            raise TimeoutError('The monitor did not accept the test event before the deadline')
        report.update(stage='waiting_for_agent_reply', event_id=event['id'],
                      accepted_turn_id=event['turn_id'], accepted_at=timestamp())
        save(report_path, report)
        while time.time() < plan['expires_at']:
            history = rpc.request('thread/items/list', {
                'threadId': plan['thread_id'], 'turnId': event['turn_id'],
                'limit': 100, 'sortDirection': 'desc'})
            matched = find_agent_ack(history.get('data', []), event['turn_id'], plan['ack_token'])
            if matched:
                report.update(stage='passed', passed=True, verified_at=timestamp(),
                              agent_response_verified=True, ack_token=plan['ack_token'],
                              matched_agent_item_id=matched['item']['id'],
                              matched_agent_turn_id=matched['turnId'],
                              reply_excerpt=matched['item']['text'][:500])
                save(report_path, report)
                return
            time.sleep(0.2)
        raise TimeoutError('The event was accepted, but no real agent acknowledgement was observed')
    except Exception as exc:
        report.update(stage='failed', passed=False, error=str(exc), finished_at=timestamp())
        save(report_path, report)
        raise
    finally:
        rpc.close()
        try:
            control(plan['state_dir'], {'method': 'unwatch', 'id': plan['watcher_id']})
        except OSError:
            pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='action', required=True)
    p = commands.add_parser('arm')
    p.add_argument('--state-dir', default=os.environ.get('CODEX_MONITOR_HOME', '~/.codex-monitor'))
    p.add_argument('--timeout', type=float, default=300)
    for name in ('observe', 'report', 'confirm'):
        p = commands.add_parser(name)
        p.add_argument('--directory', required=True)
    args = parser.parse_args()
    if args.action == 'arm':
        arm(args)
    elif args.action == 'observe':
        observe(args)
    elif args.action == 'confirm':
        directory = Path(args.directory).resolve()
        plan = json.loads((directory / 'plan.json').read_text())
        report = json.loads((directory / 'report.json').read_text())
        if not report.get('independently_observed_idle'):
            raise ValueError('Missing independent idle observation')
        rpc = CodexRPC(plan['codex_home'], plan['codex_bin'])
        try:
            page = rpc.request('thread/items/list', {'threadId': plan['thread_id'],
                'turnId': report['accepted_turn_id'], 'limit': 100, 'sortDirection': 'desc'})
            matched = find_agent_ack(page.get('data', []), report['accepted_turn_id'], plan['ack_token'])
            if not matched:
                raise ValueError('No canonical agent acknowledgement in the accepted turn')
            report.update(matched_agent_item_id=matched['item']['id'],
                          matched_agent_turn_id=matched['turnId'], confirmed_at=timestamp())
            save(directory / 'report.json', report)
            print(json.dumps(report, ensure_ascii=False, indent=2))
        finally:
            rpc.close()
    else:
        print((Path(args.directory) / 'report.json').read_text())


if __name__ == '__main__':
    main()
