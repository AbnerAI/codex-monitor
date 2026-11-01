#!/usr/bin/env python3
"""Opt-in live Codex checks. Idle mode uses a minimal isolated test thread.

This is not run by unit tests or CI. It submits real model work using the
current Codex account/model. The isolated fixture uses low effort and HTTP/SSE
to avoid reusing or changing the owning conversation's live transport.
"""
import argparse
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


def wait_for(function, timeout=90):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = function()
        if value:
            return value
        time.sleep(0.2)
    raise RuntimeError('Live check timed out')


def items(rpc, thread_id):
    result = rpc.request('thread/items/list', {'threadId': thread_id, 'limit': 50, 'sortDirection': 'desc'})
    return result.get('data', [])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=['busy', 'idle'], required=True)
    parser.add_argument('--trigger-only', action='store_true',
                        help='Idle: verify a new turn starts, without requiring a model reply')
    parser.add_argument('--output')
    args = parser.parse_args()
    home, binary = home_path(), codex_binary()
    current = os.environ['CODEX_THREAD_ID']
    received = []
    rpc = CodexRPC(home, binary, on_notification=received.append)
    fixture_thread = None
    fixture_turn = None
    try:
        parent = rpc.target(current)
        target = current
        with tempfile.TemporaryDirectory(prefix='cm-live-') as temp:
            root = Path(temp)
            if args.mode == 'idle':
                params = {'cwd': str(root), 'ephemeral': False,
                          'historyMode': parent.get('historyMode', 'paginated'),
                          'config': {'model_reasoning_effort': 'low',
                                     'model_providers': {parent.get('modelProvider', 'openai'):
                                                         {'supports_websockets': False}}}}
                if parent.get('model'):
                    params['model'] = parent['model']
                fixture_thread = rpc.request('thread/start', params)['thread']['id']
                target = fixture_thread
                print(json.dumps({'stage': 'fixture_created', 'thread_id': target,
                                  'model': parent.get('model')}), flush=True)
                prompt = ('This is a minimal delivery integration test. Reply exactly READY now. '
                          'When a subsequent codex_monitor.monitor_event tool output arrives, '
                          'reply MONITOR_ACK: followed by the content of its first event. '
                          'Do not call tools or change any files.')
                if not args.trigger_only:
                    fixture_turn = rpc.request('turn/start', {'threadId': target, 'input': [{'type': 'text', 'text': prompt}]}, mutating=True)['turn']['id']
                def initial_idle():
                    for message in list(received):
                        if message.get('method') != 'turn/completed':
                            continue
                        turn = message.get('params', {}).get('turn', {})
                        if turn.get('id') == fixture_turn:
                            if turn.get('status') != 'completed':
                                raise RuntimeError('Fixture model request failed: ' + json.dumps(turn.get('error')))
                            return rpc.target(target)['status']['type'] == 'idle'
                    return False
                try:
                    if args.trigger_only:
                        wait_for(lambda: rpc.target(target)['status']['type'] == 'idle', timeout=10)
                    else:
                        wait_for(initial_idle, timeout=60)
                except Exception:
                    print(json.dumps({'fixture_diagnostic': list(rpc.diagnostics),
                                      'thread_status': rpc.target(target)['status']}, ensure_ascii=False)[:6000], flush=True)
                    raise
            before = rpc.target(target)['status']['type']
            inbox = root / 'inbox.ndjson'
            inbox.touch()
            state = root / 'state'
            service = subprocess.Popen([sys.executable, '-B', '-m', 'codex_monitor', '--state-dir', str(state), 'run'],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            try:
                def started():
                    try:
                        return control(state, {'method': 'status'}, timeout=0.5)
                    except OSError:
                        return None
                wait_for(started, timeout=10)
                registration = control(state, {'method': 'watch', 'spec': {
                    'source': 'file', 'path': str(inbox), 'thread_id': target,
                    'codex_home': home, 'codex_bin': binary, 'interval': 0.2,
                    'name': 'codex-monitor-live-check', 'once': True}})
                marker = 'CM_CHECK_' + uuid.uuid4().hex
                inbox.write_text(json.dumps({'content': marker}) + '\n')
                def accepted():
                    events = control(state, {'method': 'events', 'id': registration['id'], 'content': True})
                    if events and events[0]['status'] == 'uncertain':
                        raise RuntimeError('Live delivery acknowledgement was lost')
                    return events[0] if events and events[0]['status'] == 'accepted' else None
                event = wait_for(accepted)
                initial_turn = fixture_turn
                if args.mode == 'idle':
                    fixture_turn = event['turn_id']
                def evidence():
                    history = list(received) if args.mode == 'idle' else items(rpc, target)
                    text = json.dumps(history, ensure_ascii=False)
                    if args.mode == 'idle' and args.trigger_only:
                        return history if event['id'] in text and event['turn_id'] != initial_turn else None
                    if args.mode == 'idle':
                        return history if 'MONITOR_ACK:' + marker in text else None
                    return history if event['id'] in text and 'functionCallOutput' in text else None
                history = wait_for(evidence)
                result = {'mode': args.mode, 'passed': True, 'initial_thread_status': before,
                          'fixture_ephemeral': False, 'thread_id': target,
                          'watcher_id': registration['id'], 'event_id': event['id'],
                          'accepted_turn_id': event['turn_id'], 'tool_output_observed': True,
                          'history_query_verified': args.mode == 'busy',
                          'idle_turn_start_verified': args.mode == 'idle',
                          'idle_agent_response_verified': args.mode == 'idle' and not args.trigger_only}
                if args.output:
                    Path(args.output).write_text(json.dumps(result, indent=2) + '\n')
                print(json.dumps(result, indent=2))
            finally:
                if service.poll() is None:
                    try:
                        control(state, {'method': 'shutdown'})
                        service.wait(timeout=20)
                    except Exception:
                        service.terminate()
                        service.wait(timeout=5)
                error = service.stderr.read().decode()
                service.stderr.close()
                if service.returncode:
                    print(error[:2000], file=sys.stderr)
    finally:
        if fixture_thread:
            if fixture_turn:
                try:
                    rpc.request('turn/interrupt', {'threadId': fixture_thread, 'turnId': fixture_turn})
                except Exception:
                    pass
            try:
                rpc.request('thread/archive', {'threadId': fixture_thread})
                rpc.request('thread/unsubscribe', {'threadId': fixture_thread})
            except Exception:
                pass
        rpc.close()


if __name__ == '__main__':
    main()
