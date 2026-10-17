import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

from .app import Desktop, check_version, codex_binary, open_selected_thread
from .controller import Controller
from .policy import quota_status
from .rpc import ReadOnlyServer
from .store import Store
from .tasks import cloud_conversations, inspect_task, list_conversations, stored_assessment


def identifier(value):
    try:
        return str(uuid.UUID(value))
    except ValueError as exc:
        raise argparse.ArgumentTypeError('An exact task UUID is required') from exc


def positive(value):
    n = int(value)
    if not 1 <= n <= 100:
        raise argparse.ArgumentTypeError('Count must be between 1 and 100')
    return n


def parser():
    p = argparse.ArgumentParser(description='Local tool that resumes Codex app tasks after usage limits reset')
    p.add_argument('--home', type=Path, default=Path(os.environ.get('CODEX_HOME', Path.home() / '.codex')))
    p.add_argument('--app', type=Path, default=Path('/Applications/ChatGPT.app'))
    p.add_argument('--state-dir', type=Path, default=Path.home() / '.codex-auto-resume')
    sub = p.add_subparsers(dest='command', required=True)
    sub.add_parser('doctor', help='Read-only check of app version, connection and real usage')
    sub.add_parser('list', help='List all local conversations across pages, including archived, plus cloud conversations the app remembers (not resumable); does not monitor anything')
    sub.add_parser('status', help='Show watch records')
    web = sub.add_parser('web', help='Start the local browser dashboard')
    web.add_argument('--port', type=int, default=8765)
    check = sub.add_parser('check', help='Read-only check of the selected conversation\'s status and eligibility')
    check.add_argument('thread', type=identifier)
    stop = sub.add_parser('stop', help='Stop future automatic resumes without interrupting a running app task')
    stop.add_argument('thread', type=identifier)
    for name in ('start', '_watch'):
        item = sub.add_parser(name, help='Monitor one selected task in the background' if name == 'start' else 'Internal background command; use start instead')
        item.add_argument('thread', type=identifier)
        item.add_argument('--max-resumes', type=positive, default=3, help='Total attempt limit for this task (default 3)')
        item.add_argument('--limit-id', default=None, help='Choose explicitly when there are multiple usage buckets; a single codex bucket is detected automatically')
        if name == '_watch':
            item.add_argument('--lock-fd', type=int, default=None, help=argparse.SUPPRESS)
    return p


def output(value):
    print(json.dumps(value, ensure_ascii=False, indent=2), flush=True)


def watch_process_spec(args, lock_fd):
    """Build an independent watcher command for the source checkout."""
    cmd = [sys.executable, '-m', 'codex_resume', '--home', str(args.home), '--app', str(args.app),
                    '--state-dir', str(args.state_dir), '_watch', args.thread,
                    '--max-resumes', str(args.max_resumes), '--lock-fd', str(lock_fd)]
    if args.limit_id:
        cmd += ['--limit-id', args.limit_id]
    return cmd, Path(__file__).resolve().parent.parent, None


def serve_watch(args, store):
    store.lock(args.thread, inherited_fd=args.lock_fd)
    quota = None
    def read_quota():
        nonlocal quota
        if quota is None:
            reader = ReadOnlyServer(codex_binary(args.app), args.home)
            reader.__enter__()
            quota = reader
        try:
            return quota.query('account/rateLimits/read')
        except Exception:
            quota.__exit__(None, None, None)
            quota = None
            raise
    def load_original(tid):
        # Revalidate eligibility before navigation as well as before dispatch.
        with ReadOnlyServer(codex_binary(args.app), args.home) as reader:
            if any(r['id'] == tid for r in list_conversations(reader, (True,))):
                return False
            saved = reader.query('thread/read', {'threadId': tid, 'includeTurns': True})
        if not stored_assessment(saved.get('thread'), tid)['canMonitor']:
            return False
        if not store.get(tid)['enabled']:
            return False
        open_selected_thread(args.app, tid)
    controller = Controller(args.thread, store, lambda: Desktop(args.home, args.app), read_quota, args.limit_id,
                            open_thread=load_original)
    previous = None
    try:
        while controller.step():
            state = store.get(args.thread)
            status = (state['status'], state['reason'])
            if status != previous:
                output({'time': time.time(), 'status': status[0], 'reason': status[1]})
                previous = status
            # Small cancellable sleeps; after wake from system sleep step() rechecks everything.
            for _ in range(30):
                if not store.get(args.thread)['enabled']:
                    return
                time.sleep(1)
        state = store.get(args.thread)
        output({'status': state['status'], 'reason': state['reason']})
    finally:
        if quota:
            quota.__exit__(None, None, None)


def main(argv=None):
    args = parser().parse_args(argv)
    args.home, args.app, args.state_dir = [p.expanduser().absolute() for p in (args.home, args.app, args.state_dir)]
    if args.command == 'web':
        from .web import serve
        serve(args)
        return
    if args.command in ('doctor', 'check', 'list', 'start', '_watch'):
        version = check_version(args.app)
    if args.command == 'check':
        result = inspect_task(args.home, args.app, args.thread)
        output(dict(result, note='Read-only check; no task was opened, monitored or messaged'))
        return
    if args.command in ('doctor', 'list'):
        with ReadOnlyServer(codex_binary(args.app), args.home) as server:
            if args.command == 'list':
                rows = list_conversations(server)
                ids = {row['id'] for row in rows}
                output(rows + [row for row in cloud_conversations(args.home) if row['id'] not in ids])
                return
            with Desktop(args.home, args.app):
                pass
            quota = server.query('account/rateLimits/read')
            ready, next_check, reason = quota_status(quota, time.time())
            output({'app': version, 'ipc': 'connected', 'quotaRead': 'ok',
                    'ready': ready, 'nextCheck': next_check, 'reason': reason,
                    'rateLimits': quota.get('rateLimits'),
                    'note': 'A successful read-only probe does not mean any specific task can be resumed'})
        return
    store = Store(args.state_dir)
    try:
        if args.command == 'status':
            rows = store.all()
            for row in rows:
                row['attempts'] = store.count(row['thread_id'])
                if row.get('pid') and row['enabled']:
                    try:
                        os.kill(row['pid'], 0)
                    except ProcessLookupError:
                        row['process'] = 'not_running'
                    else:
                        row['process'] = 'pid_exists_not_identity_verified'
            output(rows)
        elif args.command == 'stop':
            store.stop(args.thread)
            output({'stopped': args.thread, 'note': 'Only cancels future automatic resumes; messages already sent are not retracted'})
        elif args.command == '_watch':
            serve_watch(args, store)
        elif args.command == 'start':
            # Validate the exact thread before arming a background process.
            assessment = inspect_task(args.home, args.app, args.thread)
            if not assessment['canMonitor']:
                raise RuntimeError(assessment['reason'])
            if store.uncertain(args.thread):
                raise RuntimeError('This task has an uncertain send record; check manually first. It cannot be re-monitored automatically')
            if store.count(args.thread) >= args.max_resumes:
                raise RuntimeError('Total attempt limit reached; review the records before deciding whether to raise --max-resumes')
            store.lock(args.thread)
            store.arm(args.thread, args.max_resumes)
            # Keep the same flock held across process creation. The child adopts
            # this descriptor; closing only the parent's copy cannot unlock it.
            lock_fd = store.lock_file.fileno()
            log = args.state_dir / (args.thread + '.log')
            cmd, child_cwd, child_env = watch_process_spec(args, lock_fd)
            try:
                fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, 'ab') as stream:
                    child = subprocess.Popen(cmd, cwd=child_cwd, env=child_env,
                                             stdin=subprocess.DEVNULL, stdout=stream, stderr=stream,
                                             start_new_session=True, pass_fds=(lock_fd,))
            except Exception:
                store.update(args.thread, 'blocked', 'Background process failed to start; monitoring not enabled', True,
                             only_enabled=True)
                raise
            # Verify child liveness and its first ledger update, rather than claim launch from Popen alone.
            for _ in range(40):
                state = store.get(args.thread)
                if state['status'] != 'starting' or child.poll() is not None:
                    break
                time.sleep(0.1)
            if child.poll() is not None:
                if store.get(args.thread)['status'] == 'starting':
                    store.update(args.thread, 'blocked', 'Background process failed to start', True, only_enabled=True)
                raise RuntimeError('Background monitoring stopped; run status to see why')
            output({'threadId': args.thread, 'pid': child.pid, 'log': str(log),
                    'status': store.get(args.thread)['status'],
                    'note': 'Selected task only; actions are never auto-approved. The app must stay open.'})
    finally:
        store.close()


def cli():
    try:
        main()
    except KeyboardInterrupt:
        print('Operation aborted', file=sys.stderr)
        sys.exit(130)
    except Exception as exc:
        # Known local errors contain no session content. Other exceptions stay generic.
        from .app import AppError
        if isinstance(exc, (AppError, RuntimeError, ValueError)):
            print(str(exc), file=sys.stderr)
        else:
            print('Operation failed; check the app, directory permissions and network. No send was retried.', file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    cli()
