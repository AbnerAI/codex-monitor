"""Public CLI. Every control response is JSON for agents and shell scripts."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

from . import __version__
from .daemon import control, serve, validate_spec
from .rpc import CodexRPC, RpcError, codex_binary, home_path
from .store import Store
from .statusline import read_label, build_snapshot


def parser():
    ap = argparse.ArgumentParser(description='Persistent watchers that notify their owning Codex CLI thread')
    ap.add_argument('--version', action='version', version=__version__)
    ap.add_argument('--state-dir', default=os.environ.get('CODEX_MONITOR_HOME', '~/.codex-monitor'))
    sub = ap.add_subparsers(dest='action', required=True)
    for name in ('start', 'run', 'status', 'stop', 'list'):
        sub.add_parser(name)
    p = sub.add_parser('launch', help='Run interactive Codex with its own iTerm2 counter')
    p.add_argument('--codex', required=True, help='Original Codex executable, including any existing wrapper')
    p.add_argument('codex_args', nargs=argparse.REMAINDER)
    for name in ('doctor', 'watch'):
        p = sub.add_parser(name)
        p.add_argument('--thread', default=os.environ.get('CODEX_THREAD_ID'))
        p.add_argument('--codex-home', default=None)
        p.add_argument('--codex-bin', default=None)
        p.add_argument('--socket', default=None, help='Explicit owning app-server control socket')
        if name == 'watch':
            source = p.add_mutually_exclusive_group(required=True)
            source.add_argument('--file')
            source.add_argument('--command')
            p.add_argument('--cwd', default=os.getcwd())
            p.add_argument('--interval', type=float, default=2)
            p.add_argument('--timeout', type=float, default=10)
            p.add_argument('--name')
            p.add_argument('--from-start', action='store_true', help='File source: include existing complete lines')
            p.add_argument('--once', action='store_true', help='Stop after the first accepted notification batch')
    p = sub.add_parser('unwatch')
    p.add_argument('id')
    p = sub.add_parser('events')
    p.add_argument('--id', help='Watcher id')
    p.add_argument('--content', action='store_true')
    p = sub.add_parser('retry', help='Retry only after checking ambiguous deliveries in Codex history')
    p.add_argument('event_id')
    p = sub.add_parser('statusline', help='Read the current thread count without model work')
    p.add_argument('--thread', default=os.environ.get('CODEX_THREAD_ID'))
    p.add_argument('--codex-home', default=None)
    p.add_argument('--show-zero', action='store_true')
    p.add_argument('--json', action='store_true')
    p = sub.add_parser('iterm', help='Publish a counter variable in the current iTerm2 session')
    iterm = p.add_subparsers(dest='iterm_action', required=True)
    for name in ('attach', 'status', 'detach', 'api-status', 'identify'):
        item = iterm.add_parser(name)
        item.add_argument('--session', default=None)
        if name == 'attach':
            item.add_argument('--thread', default=os.environ.get('CODEX_THREAD_ID'))
            item.add_argument('--codex-home', default=None)
    item = iterm.add_parser('run', help=argparse.SUPPRESS)
    item.add_argument('--attachment', required=True)
    return ap


def start(root):
    try:
        return control(root, {'method': 'status'}, timeout=1)
    except (OSError, RuntimeError):
        pass
    Store(root)
    env = os.environ.copy()
    package_root = str(Path(__file__).resolve().parent.parent)
    env['PYTHONPATH'] = package_root + (os.pathsep + env['PYTHONPATH'] if env.get('PYTHONPATH') else '')
    with (root / 'startup.log').open('ab') as log:
        process = subprocess.Popen([sys.executable, '-m', 'codex_monitor', '--state-dir', str(root), 'run'],
                                   cwd=package_root, env=env, stdin=subprocess.DEVNULL,
                                   stdout=log, stderr=log, start_new_session=True, close_fds=True)
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        try:
            return control(root, {'method': 'status'}, timeout=0.5)
        except (OSError, RuntimeError):
            if process.poll() is not None:
                break
            time.sleep(0.05)
    raise RuntimeError('Service failed to start; inspect ' + str(root / 'startup.log'))


def doctor(args):
    binary, home = codex_binary(args.codex_bin), home_path(args.codex_home)
    rpc = CodexRPC(home, binary, args.socket)
    result = {'codex_bin': binary, 'codex_home': home, 'socket': rpc.socket_path,
              'thread_id': args.thread, 'monitor_version': __version__}
    try:
        rpc.connect()
        result['app_server_connected'] = True
        if args.thread:
            target = rpc.target(args.thread)
            result['thread_status'] = target.get('status')
            result['can_accept_direct_input'] = target.get('canAcceptDirectInput')
            result['exact_thread_verified'] = target.get('id') == args.thread
        else:
            result['exact_thread_verified'] = False
            result['note'] = 'Supply --thread or invoke from a Codex shell with CODEX_THREAD_ID'
    finally:
        rpc.close()
    return result


def execute(args):
    root = Path(args.state_dir).expanduser().resolve()
    if args.action == 'iterm':
        from . import iterm
        return iterm.execute(root, args)
    if args.action == 'statusline':
        if not args.thread:
            raise ValueError('--thread or CODEX_THREAD_ID is required')
        try:
            snapshot = json.loads((root / 'statusline.json').read_text())
        except (OSError, ValueError):
            snapshot = {}
        zero_label = read_label(snapshot, args.thread, home_path(args.codex_home), show_zero=True)
        if not zero_label:
            # Older running services do not export snapshots. Read their existing
            # control API instead of restarting a service shared by other shells.
            try:
                rows = control(root, {'method': 'list'}, timeout=0.5)
                watchers = [{'active': row.get('active'), 'spec': row} for row in rows]
                snapshot = build_snapshot(watchers)
                zero_label = read_label(snapshot, args.thread, home_path(args.codex_home), show_zero=True)
            except (OSError, RuntimeError, ValueError):
                snapshot = {}
        return {'label': read_label(snapshot, args.thread, home_path(args.codex_home), show_zero=args.show_zero),
                'available': bool(zero_label), 'active': int(zero_label.split(' ', 1)[0]) if zero_label else None,
                'thread_id': args.thread, 'codex_home': home_path(args.codex_home)}
    if args.action == 'doctor':
        return doctor(args)
    if args.action == 'run':
        serve(root)
        return {'stopped': True}
    if args.action == 'start':
        return start(root)
    if args.action == 'status':
        try:
            return control(root, {'method': 'status'}, timeout=1)
        except (FileNotFoundError, ConnectionRefusedError):
            return {'running': False, 'state_dir': str(root)}
    if args.action == 'stop':
        result = control(root, {'method': 'shutdown'})
        # Wait for the control endpoint to disappear; do not kill a saved PID.
        deadline = time.monotonic() + 22
        while time.monotonic() < deadline:
            try:
                control(root, {'method': 'status'}, timeout=0.3)
            except (OSError, RuntimeError):
                return dict(result, stopped=True)
            time.sleep(0.1)
        raise RuntimeError('Service is still shutting down; check status')
    if args.action == 'watch':
        if not args.thread:
            raise ValueError('--thread or CODEX_THREAD_ID is required; never guess the latest session')
        spec = {'source': 'file' if args.file else 'command', 'thread_id': args.thread,
                'codex_home': home_path(args.codex_home), 'codex_bin': codex_binary(args.codex_bin),
                'socket': str(Path(args.socket).expanduser().resolve()) if args.socket else None,
                'interval': args.interval, 'timeout': args.timeout, 'name': args.name,
                'from_start': args.from_start, 'once': args.once}
        if args.file:
            spec['path'] = str(Path(args.file).expanduser().absolute())
        else:
            spec.update(command=args.command, cwd=str(Path(args.cwd).expanduser().resolve()))
        validate_spec(spec)
        # The launching client owns its display. Tool subprocess environments
        # may belong to another terminal, including on a resumed thread.
        return control(root, {'method': 'watch', 'spec': spec})
    request = {'method': {'stop': 'shutdown'}.get(args.action, args.action)}
    for key in ('id', 'content', 'event_id'):
        if hasattr(args, key):
            request[key] = getattr(args, key)
    return control(root, request)


def main():
    if os.name != 'posix':
        raise SystemExit('codex-monitor currently supports macOS/Linux (POSIX); Windows users can use WSL')
    args = parser().parse_args()
    try:
        if args.action == 'launch':
            from .launch import launch
            root = Path(args.state_dir).expanduser().resolve()
            raise SystemExit(launch(root, args.codex, args.codex_args))
        result = execute(args)
        if args.action == 'statusline' and not args.json:
            print(result['label'])
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2))
    except (OSError, ValueError, RpcError, RuntimeError) as exc:
        error = {'error': str(exc)}
        if isinstance(getattr(exc, 'details', None), dict):
            error.update(exc.details)
        print(json.dumps(error, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)

