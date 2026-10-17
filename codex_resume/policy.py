"""Pure decisions. Unknown state never authorizes a continuation."""
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path


class UnsupportedState(ValueError):
    pass


@dataclass(frozen=True)
class Decision:
    action: str
    reason: str
    turn_id: str = ''
    task_state: str = 'unknown'


def latest_turn(state):
    history = state.get('turnHistory')
    if isinstance(history, dict) and history.get('kind') == 'canonical':
        hist = history.get('history', {})
        islands = hist.get('islands', [])
        if not islands:
            raise UnsupportedState('No verifiable recent conversation history')
        tail = islands[-1]
        if tail.get('newerBoundary', {}).get('status') != 'exhausted':
            raise UnsupportedState('History tail incomplete; cannot determine the latest turn')
        entries = tail.get('entries', [])
        if not entries:
            raise UnsupportedState('Latest history segment is empty')
        turn = hist.get('entitiesByKey', {}).get(entries[-1].get('value'))
    else:
        turns = state.get('turns')
        if not isinstance(turns, list) or not turns:
            raise UnsupportedState('Missing conversation turns')
        turn = turns[-1]
    if not isinstance(turn, dict):
        raise UnsupportedState('Unsupported conversation history structure')
    return turn


def quota_failure(turn):
    error = turn.get('error')
    if not isinstance(error, dict):
        return False
    info = error.get('codexErrorInfo')
    # These are protocol enum values, not a fuzzy match on user/assistant text.
    return info in ('usageLimitExceeded', 'UsageLimitExceeded') if isinstance(info, str) else False



def interrupted_context_picker(state, request):
    """Recognize ONLY a verified orphan of the App's canceled folder picker.

    No approval request, other tool, current turn, unknown turn, or merely old
    request qualifies. Evidence must be in the same contiguous canonical tail
    as the latest turn. This does not answer or remove the request in the App.
    """
    if not isinstance(request, dict) or request.get('method') != 'item/tool/call':
        return False
    params = request.get('params')
    if not isinstance(params, dict):
        return False
    if params.get('namespace') != 'codex_app' or params.get('tool') != 'setup_codex_context_picker':
        return False
    if not isinstance(state.get('id'), str) or params.get('threadId') != state['id']:
        return False
    tid = params.get('turnId')
    if not isinstance(tid, str) or not tid:
        return False
    history = state.get('turnHistory')
    if not isinstance(history, dict) or history.get('kind') != 'canonical':
        return False
    hist = history.get('history')
    if not isinstance(hist, dict):
        return False
    islands, entities = hist.get('islands'), hist.get('entitiesByKey')
    if not isinstance(islands, list) or not islands or not isinstance(entities, dict):
        return False
    tail = islands[-1]
    if not isinstance(tail, dict) or not isinstance(tail.get('newerBoundary'), dict):
        return False
    entries = tail.get('entries')
    if tail['newerBoundary'].get('status') != 'exhausted' or not isinstance(entries, list) or len(entries) < 2:
        return False
    turns = []
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get('value'), str):
            return False
        turn = entities.get(entry['value'])
        if not isinstance(turn, dict) or not isinstance(turn.get('turnId'), str):
            return False
        turns.append(turn)
    if turns[-1]['turnId'] == tid:
        return False
    matches = [turn for turn in turns[:-1] if turn['turnId'] == tid]
    return len(matches) == 1 and matches[0].get('status') == 'interrupted'


# Hosts whose conversations this tool may resume. `durable` is the App's cloud
# ("Long-lived") host; its conversations are stored server-side.
SUPPORTED_HOSTS = ('local', 'durable')


def persisted(state):
    """True only for a conversation that is saved, not ephemeral.

    Older Apps send `ephemeral: false`. Since 26.1013 the field is omitted, so a
    missing value is accepted only when the conversation's rollout file exists,
    or when it lives on the server-backed `durable` host.
    """
    if 'ephemeral' in state:
        return state['ephemeral'] is False
    if state.get('hostId') == 'durable':
        return True
    path = state.get('rolloutPath')
    return isinstance(path, str) and bool(path) and Path(path).is_file()


def decide(state):
    if not isinstance(state, dict) or state.get('hostId') not in SUPPORTED_HOSTS:
        return Decision('stop', 'Only local and cloud (durable) app conversations are supported', task_state='unsupported')
    if not persisted(state):
        return Decision('stop', 'Ephemeral or unknown conversation types are not supported', task_state='unsupported')
    if state.get('resumeState') != 'resumed':
        return Decision('wait', 'Conversation still loading; waiting for status to sync', task_state='connecting')
    if not isinstance(state.get('requests'), list):
        return Decision('stop', 'Missing approval/user-input state')
    if any(not interrupted_context_picker(state, request) for request in state['requests']):
        return Decision('stop', 'The task is waiting for an approval or user input', task_state='needs_attention')
    if state.get('threadGoalResumeConfirmation'):
        return Decision('stop', 'Resuming the task needs user confirmation', task_state='needs_attention')
    goal = state.get('threadGoal')
    if goal is not None and (not isinstance(goal, dict) or goal.get('status') != 'active'):
        return Decision('stop', 'The task goal is paused, blocked or finished', task_state='needs_attention')
    # Since 26.1013 queued follow-ups live only in the App UI and are absent here.
    if state.get('queuedFollowUps'):
        return Decision('stop', 'A user message is already queued', task_state='needs_attention')
    runtime = state.get('threadRuntimeStatus')
    if not isinstance(runtime, dict):
        return Decision('stop', 'Missing runtime status')
    runtime_type = runtime.get('type')
    if runtime_type == 'active':
        if runtime.get('activeFlags'):
            return Decision('stop', 'The task is waiting for an approval, input or another precondition', task_state='needs_attention')
        return Decision('wait', 'The task is running; once monitored, it will wait for the reset if the usage limit stops it', task_state='running')
    # A real usage-limit failure is exposed by the pinned App as systemError,
    # not idle.  Only these two known terminal containers may be interpreted;
    # other/transitional runtime types retain enrollment but never authorize a send.
    if runtime_type not in ('idle', 'systemError'):
        return Decision('wait', 'Task runtime status is syncing; keeping the watch and rechecking shortly', task_state='connecting')
    try:
        turn = latest_turn(state)
    except UnsupportedState as exc:
        return Decision('stop', str(exc))
    tid = turn.get('turnId')
    if not isinstance(tid, str) or not tid:
        return Decision('stop', 'The latest turn has no stable ID')
    status = turn.get('status')
    if status == 'inProgress':
        return Decision('wait', 'This turn\'s status is syncing', task_state='running')
    if status == 'completed':
        if isinstance(goal, dict) and goal.get('status') == 'active':
            return Decision('wait', 'The long-task goal is still active; waiting for the app\'s next step. No extra message is sent because this turn ended', tid, 'running')
        return Decision('stop', 'This turn finished and nothing is running; nothing to monitor', tid, 'idle')
    if status == 'interrupted':
        return Decision('stop', 'This turn was stopped; it will not be restarted automatically', tid, 'interrupted')
    if status != 'failed' or not quota_failure(turn):
        return Decision('stop', 'Not a usage-limit error with structured evidence', tid, 'other_failure')
    return Decision('resume', 'The task paused because the usage limit ran out; it will continue in the original conversation once usage is confirmed reset', tid, 'quota_limited')


def fingerprint(state):
    """Ignore token counters and titles; bind the actual work and execution settings."""
    keys = ('id', 'hostId', 'cwd', 'latestModel', 'latestReasoningEffort',
            'latestCollaborationMode', 'currentPermissions', 'latestThreadSettings',
            'threadGoal', 'threadGoalResumeConfirmation', 'requests', 'queuedFollowUps',
            'threadRuntimeStatus', 'resumeState')
    data = {k: state.get(k) for k in keys}
    data['turn'] = latest_turn(state)
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def numeric(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def quota_status(response, now, limit_id=None):
    """Return (ready, next_check_epoch, explanation). Always re-read after reset."""
    buckets = response.get('rateLimitsByLimitId')
    if isinstance(buckets, dict) and buckets:
        if limit_id is None and set(buckets) != {'codex'}:
            return False, None, 'There are multiple usage buckets; specify --limit-id explicitly'
        bucket = buckets.get(limit_id or 'codex')
    else:
        bucket = response.get('rateLimits')
        if isinstance(bucket, dict) and limit_id and bucket.get('limitId') != limit_id:
            bucket = None
    if not isinstance(bucket, dict):
        return False, None, 'The selected usage bucket is missing'
    if bucket.get('spendControlReached') or bucket.get('individualLimit') is not None:
        return False, None, 'The account has a separate usage limit; check manually'
    reached = bucket.get('rateLimitReachedType')
    if reached not in (None, 'rate_limit_reached'):
        return False, None, 'The account has a non-window usage limit'
    exhausted = []
    windows = [bucket.get('primary'), bucket.get('secondary')]
    if windows[0] is None:
        return False, None, 'Missing primary window usage data'
    for window in windows:
        if window is None:
            continue
        if not isinstance(window, dict):
            return False, None, 'Unsupported usage window structure'
        used, reset, duration = (window.get(k) for k in ('usedPercent', 'resetsAt', 'windowDurationMins'))
        if not numeric(used) or not 0 <= used <= 100 or not numeric(reset) or reset <= 0 or not numeric(duration) or duration <= 0:
            return False, None, 'Usage data is incomplete; can\'t infer when it becomes available'
        if used >= 100:
            exhausted.append(reset)
        elif reset <= now:
            return False, now + 60, 'Usage data is stale; re-reading shortly'
    if exhausted:
        return False, max(now + 60, max(exhausted) + 15), 'Usage still exhausted; waiting for the actual reset before checking again'
    if reached:
        return False, now + 60, 'The service still reports the limit as reached; checking again shortly'
    return True, now, 'All windows in the selected usage bucket have capacity'
