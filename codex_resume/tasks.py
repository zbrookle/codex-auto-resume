"""Conversation discovery and enrollment, separate from permission to send."""
import json
from pathlib import Path
import socket
import time
import uuid

from .app import ConnectionUnavailable, Desktop, codex_binary
from .policy import decide, interrupted_context_picker, quota_failure
from .rpc import ReadOnlyServer

SOURCES = ['cli', 'vscode', 'exec', 'appServer', 'subAgent', 'subAgentReview',
           'subAgentCompact', 'subAgentThreadSpawn', 'subAgentOther', 'unknown']


def list_conversations(server, archive_filters=(False, True)):
    """Consume all pages, including archives; don't silently return only page one."""
    rows, ids = [], set()
    deadline = time.monotonic() + 55
    for archived in archive_filters:
        cursor, seen = None, set()
        while True:
            if time.monotonic() >= deadline:
                raise RuntimeError('Conversation list not fully read yet; try again shortly')
            params = {'limit': 100, 'archived': archived, 'sourceKinds': SOURCES,
                      'sortKey': 'updated_at', 'modelProviders': []}
            if cursor is not None:
                params['cursor'] = cursor
            page = server.query('thread/list', params)
            if not isinstance(page, dict) or not isinstance(page.get('data'), list) or 'nextCursor' not in page:
                raise RuntimeError('Conversation list incomplete; later pages could not be confirmed')
            for row in page['data']:
                if not isinstance(row, dict) or not isinstance(row.get('id'), str):
                    raise RuntimeError('Unsupported conversation list format')
                if row['id'] not in ids:
                    ids.add(row['id'])
                    rows.append({key: row.get(key) for key in ('id', 'name', 'preview', 'cwd', 'updatedAt')})
                    rows[-1]['archived'] = archived
            cursor = page['nextCursor']
            if cursor is None:
                break
            if not isinstance(cursor, str) or not cursor or cursor in seen:
                raise RuntimeError('Unexpected pagination cursor; not returning an incomplete list')
            seen.add(cursor)
    return rows


def cloud_conversations(home):
    """Cloud-hosted conversations the App remembers in its local state.

    Their live state and resumption go through the App with host-scoped
    requests; this list only supplies ids, hosts and labels. Unreadable or
    unexpected state yields an empty list.
    """
    try:
        state = json.loads((Path(home) / '.codex-global-state.json').read_text())
    except (OSError, ValueError):
        return []
    if not isinstance(state, dict):
        return []
    atoms = state.get('electron-persisted-atom-state')
    atoms = atoms if isinstance(atoms, dict) else {}
    hosts = {}
    membership = state.get('thread-project-membership-host-ids')
    if isinstance(membership, dict):
        hosts.update({t: h for t, h in membership.items() if isinstance(h, str) and h and h != 'local'})
    prototypes = atoms.get('cloud-thread-prototype-threads-by-account-v1')
    for threads in (prototypes.values() if isinstance(prototypes, dict) else ()):
        for t in (threads if isinstance(threads, dict) else ()):
            hosts.setdefault(t, 'durable')
    descriptions = atoms.get('thread-descriptions-v1')
    descriptions = descriptions if isinstance(descriptions, dict) else {}
    activity = atoms.get('thread-user-activity-times-v1')
    activity = activity if isinstance(activity, dict) else {}
    rows = []
    for tid, host in hosts.items():
        try:
            tid = str(uuid.UUID(tid))
        except (ValueError, TypeError, AttributeError):
            continue
        seen = activity.get(json.dumps([host, tid], separators=(',', ':')))
        name = descriptions.get(tid)
        rows.append({'id': tid, 'name': name if isinstance(name, str) and name else None,
                     'preview': 'Cloud conversation', 'cwd': None,
                     'updatedAt': seen / 1000 if isinstance(seen, (int, float)) else None,
                     'archived': False, 'host': host})
    return sorted(rows, key=lambda r: r['updatedAt'] or 0, reverse=True)


def thread_host(home, thread_id):
    """The App host for a conversation: a remembered cloud host, otherwise local."""
    return next((r['host'] for r in cloud_conversations(home) if r['id'] == thread_id), 'local')


def live_assessment(state, thread_id, host):
    decision = decide(state)
    return {'threadId': thread_id, 'title': state.get('title'), 'host': host,
            'decision': decision.action, 'reason': decision.reason,
            'taskState': decision.task_state,
            'canMonitor': decision.task_state in ('running', 'quota_limited'),
            'source': 'live', 'connection': 'connected', 'checkedAt': time.time(),
            'runtime': state.get('threadRuntimeStatus'), 'model': state.get('latestModel'),
            'ignoredInterruptedPickerRequests': sum(interrupted_context_picker(state, r)
                for r in state.get('requests', [])) if isinstance(state.get('requests'), list) else 0}


def inspect_cloud_task(home, app_path, row):
    """Cloud conversations have no local history, so only live App state counts."""
    try:
        with Desktop(home, app_path, host=row['host']) as desktop:
            state = desktop.snapshot(row['id'])
        if decide(state).task_state != 'connecting':
            return live_assessment(state, row['id'], row['host'])
    except (ConnectionUnavailable, ConnectionError, FileNotFoundError, TimeoutError, socket.timeout):
        pass
    return {'threadId': row['id'], 'title': row.get('name') or row.get('preview'), 'host': row['host'],
            'decision': 'stop', 'canMonitor': False, 'taskState': 'connecting',
            'source': 'live', 'connection': 'waiting', 'checkedAt': time.time(),
            'reason': 'Open this cloud conversation in the app once so its live status can be checked'}


def stored_assessment(thread, thread_id):
    """Persisted turns may qualify for enrollment, NEVER for dispatch."""
    if not isinstance(thread, dict) or thread.get('id') != thread_id:
        raise RuntimeError('The read result does not match the selected conversation')
    result = {'threadId': thread_id, 'title': thread.get('name') or thread.get('preview'),
              'source': 'history', 'connection': 'waiting', 'decision': 'stop',
              'canMonitor': False, 'taskState': 'unknown',
              'reason': 'Task status cannot be confirmed right now; will not auto-resume', 'checkedAt': time.time()}
    if thread.get('ephemeral') is not False:
        return result
    turns = thread.get('turns')
    if not isinstance(turns, list):
        return result
    if not turns:
        return dict(result, taskState='empty', reason='This conversation has no task yet; nothing to monitor')
    turn = turns[-1]
    if not isinstance(turn, dict) or not isinstance(turn.get('id'), str) or not turn['id']:
        return result
    status = turn.get('status')
    if status == 'completed':
        return dict(result, taskState='idle', reason='The latest turn finished and nothing is running; nothing to monitor')
    if status == 'interrupted':
        return dict(result, taskState='interrupted', reason='The latest turn was stopped; it will not be restarted automatically')
    if status == 'inProgress' or (status == 'failed' and quota_failure(turn)):
        return dict(result, taskState='running' if status == 'inProgress' else 'quota_limited',
                    decision='wait', canMonitor=True,
                    reason='History shows the task is still running' if status == 'inProgress' else 'History shows the task paused because the usage limit ran out')
    if status == 'failed':
        return dict(result, taskState='other_failure', reason='The task stopped on a different error; that is outside usage-reset resuming')
    return result


def inspect_task(home, app_path, thread_id):
    thread_id = str(uuid.UUID(thread_id))
    cloud = next((r for r in cloud_conversations(home) if r['id'] == thread_id), None)
    if cloud:
        return inspect_cloud_task(home, app_path, cloud)
    # Archiving is a user stop signal, even if the last persisted turn was still
    # in progress. Never navigate to or enroll an archived conversation.
    with ReadOnlyServer(codex_binary(app_path), home) as server:
        archived = next((r for r in list_conversations(server, (True,)) if r['id'] == thread_id), None)
    if archived:
        return {'threadId': thread_id, 'title': archived.get('name') or archived.get('preview'),
                'decision': 'stop', 'canMonitor': False, 'taskState': 'archived',
                'source': 'history', 'connection': 'not_checked', 'checkedAt': time.time(),
                'reason': 'This conversation is archived; it will not be monitored or opened automatically'}
    try:
        with Desktop(home, app_path) as desktop:
            state = desktop.snapshot(thread_id)
        if decide(state).task_state != 'connecting':
            return live_assessment(state, thread_id, 'local')
    except (ConnectionUnavailable, ConnectionError, FileNotFoundError, TimeoutError, socket.timeout):
        pass
    with ReadOnlyServer(codex_binary(app_path), home) as server:
        response = server.query('thread/read', {'threadId': thread_id, 'includeTurns': True})
    return stored_assessment(response.get('thread'), thread_id)
