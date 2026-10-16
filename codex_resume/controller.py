"""A single explicit watch, never a blanket scan-and-resume of all tasks."""
import time
import socket

from .app import ConnectionUnavailable
from .policy import decide, fingerprint, quota_status


class Controller:
    def __init__(self, thread_id, store, desktop_factory, quota_reader, limit_id=None, clock=time.time,
                 open_thread=None):
        self.thread_id, self.store = thread_id, store
        self.desktop_factory, self.quota_reader = desktop_factory, quota_reader
        self.limit_id, self.clock = limit_id, clock
        self.next_quota_check = 0
        self.waiting_baseline = None
        self.failures = 0
        self.open_thread = open_thread
        self.next_open = 0

    def step(self):
        watch = self.store.get(self.thread_id)
        if not watch or not watch['enabled']:
            return False
        if self.store.uncertain(self.thread_id):
            return self.end('uncertain', 'There is an unconfirmed send; check in the app. Automatic resend is blocked')
        stage = 'app'
        try:
            with self.desktop_factory() as desktop:
                state = desktop.snapshot(self.thread_id)
                decision = decide(state)
                if decision.action == 'stop':
                    status = 'needs_attention' if decision.task_state == 'needs_attention' else 'stopped'
                    return self.end(status, decision.reason)
                if decision.action == 'wait':
                    self.failures = 0
                    self.store.update(self.thread_id, 'watching' if decision.task_state == 'running' else 'waiting_connection', decision.reason, only_enabled=True)
                    return True
                baseline = fingerprint(state)
                previous = self.store.attempt(self.thread_id, decision.turn_id)
                if previous:
                    if previous['state'] == 'sent' and self.clock() - previous['created'] < 120:
                        self.store.update(self.thread_id, 'watching', 'Resume sent; waiting for the app to update its status', only_enabled=True)
                        return True
                    return self.end('stopped', 'A resume was already attempted for this turn; will not send again')
                if self.waiting_baseline is not None and self.waiting_baseline != baseline:
                    return self.end('changed', 'The task or its settings changed while waiting; choose again whether to monitor it')
                self.waiting_baseline = baseline
                if self.store.count(self.thread_id) >= watch['max_resumes']:
                    return self.end('budget', 'Reached the total automatic resume limit for this task')
                if self.clock() < self.next_quota_check:
                    self.store.update(self.thread_id, 'waiting_quota', 'Waiting for the usage reset; still checking whether the task is stopped', only_enabled=True)
                    return True
                stage = 'quota'
                ready, next_check, reason = quota_status(self.quota_reader(), self.clock(), self.limit_id)
                self.failures = 0
                if not ready:
                    if next_check is None:
                        return self.end('blocked', reason)
                    self.next_quota_check = next_check
                    self.store.update(self.thread_id, 'waiting_quota', reason, only_enabled=True)
                    return True
                # Persist intent BEFORE any network write. Includes a cancellation/budget check.
                message_id = self.store.claim(self.thread_id, decision.turn_id, baseline)
                if message_id is None:
                    return self.end('stopped', 'Monitoring paused, budget used up, or a send is already recorded for this turn')
                try:
                    # The adapter obtains another fresh snapshot and rechecks the fingerprint.
                    # It inherits App settings and sends no approval decisions.
                    if not self.store.get(self.thread_id)['enabled']:
                        return self.end('uncertain', 'Send canceled; duplicate-prevention record kept')
                    resumed = desktop.resume(self.thread_id, baseline, message_id,
                        dispatch_guard=lambda: self.store.can_dispatch(
                            self.thread_id, decision.turn_id, message_id))
                    self.store.acknowledged(self.thread_id, decision.turn_id, resumed)
                except Exception:
                    return self.end('uncertain', 'The resume was not verifiably confirmed; check in the app. It will not be resent')
                self.waiting_baseline = None
                self.next_quota_check = 0
                self.store.update(self.thread_id, 'resumed', 'The app confirmed the next turn started; continuing to watch', only_enabled=True)
                return True
        except (ConnectionUnavailable, ConnectionError, FileNotFoundError, TimeoutError, socket.timeout) as exc:
            # Keep the explicit enrollment through temporary disconnections and
            # sleeps. Never use persisted history to authorize a send.
            # A selected task may lose its owner because the App was closed, not
            # only because its page was unloaded.  The callback performs a fresh
            # read-only eligibility check before opening the exact original UUID.
            if stage == 'app' and self.open_thread and self.clock() >= self.next_open:
                self.next_open = self.clock() + 300
                if self.store.get(self.thread_id)['enabled']:
                    try:
                        if self.open_thread(self.thread_id) is False:
                            return self.end('stopped', 'The selected conversation finished, was archived or is no longer eligible; nothing was opened or sent')
                    except Exception:
                        pass  # Navigation failure is not delivery uncertainty.
            status = 'waiting_connection' if stage == 'app' else 'waiting_quota'
            reason = ('Monitoring kept; waiting for the app to reconnect. Nothing is sent until the connection is back and re-verified'
                      if stage == 'app' else 'Monitoring kept; waiting to re-read usage. Will not resume on a timer alone')
            self.store.update(self.thread_id, status, reason, only_enabled=True)
            return True
        except Exception:
            self.failures += 1
            if self.failures >= 3:
                return self.end('blocked', 'Could not safely read the app or usage three times in a row; check the app, sign-in, network and version')
            self.store.update(self.thread_id, 'retrying', 'Status unavailable right now; not resuming, will recheck shortly', only_enabled=True)
            return True

    def end(self, status, reason):
        self.store.update(self.thread_id, status, reason, True, only_enabled=True)
        return False
