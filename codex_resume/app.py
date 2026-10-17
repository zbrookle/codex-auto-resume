"""Protocol-checked Desktop IPC. Never owns threads or responds to approvals."""
import json
import mmap
import os
from pathlib import Path
import plistlib
import re
import select
import socket
import stat
import struct
import subprocess
import time
import uuid

from .policy import decide, fingerprint

# The IPC message versions this client speaks. Instead of pinning an App release,
# check_version() requires the installed App to declare these exact versions.
PROTOCOL = {'thread-stream-state-changed': 11, 'thread-stream-following-changed': 1,
            'thread-owner-discovery': 1, 'thread-follower-start-turn': 2,
            'thread-follower-read-model-settings': 1}
# Conversations on another host (e.g. the cloud `durable` host) are reached with
# host-scoped follower requests, which the App versions one above the table.
HOST_SCOPED_VERSION_OFFSET = 1
_protocol_cache = {}
MAX_FRAME = 32 * 1024 * 1024
CONTINUATION = ('Your usage limit has reset. Continue the unfinished work in this conversation. '
                'First check current progress and the workspace, do not repeat completed steps, '
                'do not expand scope, and do not bypass approvals. If the work is already done or '
                'needs a user decision, say so clearly and stop.')


def codex_binary(app_path):
    # Local patch: newer ChatGPT.app builds moved the CLI into a nested bundle.
    path = Path(app_path)
    nested = path / 'Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex'
    return nested if nested.is_file() else path / 'Contents/Resources/codex'


class AppError(RuntimeError):
    pass


class ConnectionUnavailable(AppError):
    """Transient loss of transport; never evidence authorizing a send."""


class ThreadUnavailable(ConnectionUnavailable):
    """App is connected, but the selected conversation is not loaded."""


def app_protocol(app_path):
    """Read the IPC method-version table the App bundle declares.

    Every copy of the table in app.asar must parse and agree; anything else is
    treated as an unknown protocol. Cached per bundle file identity.
    """
    asar = Path(app_path) / 'Contents/Resources/app.asar'
    try:
        st = asar.stat()
    except OSError as exc:
        raise AppError('App bundle has no app.asar; cannot verify its protocol') from exc
    key = (str(asar), st.st_ino, st.st_size, st.st_mtime_ns)
    if key in _protocol_cache:
        return _protocol_cache[key]
    tables = set()
    marker = b'"thread-follower-start-turn":'
    with open(asar, 'rb') as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as data:
        i = data.find(marker)
        while i != -1:
            start, end = data.rfind(b'{', max(0, i - 4096), i), data.find(b'}', i, i + 4096)
            try:
                table = json.loads(data[start:end + 1]) if start != -1 and end != -1 else None
            except ValueError:
                table = None
            if not isinstance(table, dict):
                raise AppError('Could not parse the App protocol table; stopping')
            tables.add(json.dumps({k: table.get(k) for k in PROTOCOL}, sort_keys=True))
            i = data.find(marker, i + 1)
    if len(tables) != 1:
        raise AppError('App protocol table missing or inconsistent; stopping')
    _protocol_cache[key] = json.loads(tables.pop())
    return _protocol_cache[key]


def check_version(app_path):
    path = Path(app_path)
    try:
        info = plistlib.loads((path / 'Contents/Info.plist').read_bytes())
    except (OSError, ValueError) as exc:
        raise AppError('No recognizable Codex app found') from exc
    if info.get('CFBundleIdentifier') != 'com.openai.codex':
        raise AppError('No recognizable Codex app found')
    protocol = app_protocol(path)
    if protocol != PROTOCOL:
        changed = sorted(k for k in PROTOCOL if protocol.get(k) != PROTOCOL[k])
        raise AppError('App IPC protocol changed (' + ', '.join(changed) + '); stopping. '
                       'This client must be updated for the new protocol first')
    return {'version': info.get('CFBundleShortVersionString'), 'build': info.get('CFBundleVersion')}


def valid_host(host):
    if not isinstance(host, str) or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', host):
        raise AppError('Unsupported conversation host')
    return host


def open_selected_thread(app_path, thread_id, host='local'):
    """Navigate to an existing selected conversation. Never prefill/send a prompt.

    The App's localConversation deep-link handler checks thread existence
    then navigates to its original route. This does not make this client an owner.
    Success here only means navigation was requested; a fresh snapshot is required.
    """
    check_version(app_path)
    target = 'codex://threads/' + str(uuid.UUID(thread_id))
    if valid_host(host) != 'local':
        target += '?hostId=' + host
    subprocess.run(['/usr/bin/open', '-g', '-a', str(app_path), target],
                   check=True, timeout=5, stdin=subprocess.DEVNULL,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class Desktop:
    def __init__(self, home, app_path='/Applications/ChatGPT.app', timeout=12, host='local'):
        self.home, self.app_path, self.timeout = Path(home), Path(app_path), timeout
        self.host = valid_host(host)
        self.sock = None
        self.client_id = 'initializing-client'
        self.owner = None
        self.thread_id = None
        self.snapshot_value = None
        self.revision = None
        self.state_generation = 0

    def __enter__(self):
        check_version(self.app_path)
        path = self.home / 'ipc/ipc.sock'
        try:
            directory, endpoint = path.parent.lstat(), path.lstat()
            if not stat.S_ISDIR(directory.st_mode) or not stat.S_ISSOCK(endpoint.st_mode):
                raise AppError('The IPC path is not a socket in a regular directory')
            if any(s.st_uid != os.getuid() or s.st_mode & 0o077 for s in (directory, endpoint)):
                raise AppError('IPC permissions or owner are unsafe; permissions will not be changed automatically')
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.settimeout(self.timeout)
            self.sock.connect(str(path))
            result = self.request('initialize', {'clientType': 'codex-auto-resume'}, 0)
            self.client_id = result['result']['clientId']
            return self
        except Exception:
            self.close()
            raise

    def close(self):
        if self.sock:
            self.sock.close()
            self.sock = None

    def __exit__(self, *args):
        self.close()

    def _send(self, msg):
        data = json.dumps(msg, ensure_ascii=False).encode()
        if not 0 < len(data) <= MAX_FRAME:
            raise AppError('IPC message too large')
        self.sock.sendall(struct.pack('<I', len(data)) + data)

    def _exact(self, n, deadline):
        chunks, remaining = [], n
        while remaining:
            timeout = deadline - time.monotonic()
            if timeout <= 0:
                raise TimeoutError('App response timed out')
            self.sock.settimeout(timeout)
            chunk = self.sock.recv(remaining)
            if not chunk:
                raise ConnectionUnavailable('App connection lost')
            chunks.append(chunk)
            remaining -= len(chunk)
        return b''.join(chunks)

    def _read(self, deadline):
        size = struct.unpack('<I', self._exact(4, deadline))[0]
        if not 0 < size <= MAX_FRAME:
            raise AppError('Unsupported IPC message length')
        msg = json.loads(self._exact(size, deadline))
        if not isinstance(msg, dict):
            raise AppError('IPC message is not an object')
        if msg.get('type') == 'client-discovery-request':
            self._send({'type': 'client-discovery-response', 'requestId': msg['requestId'],
                        'response': {'canHandle': False}})
        if msg.get('type') == 'request':
            # In particular: never become an approval handler or thread owner.
            self._send({'type': 'response', 'requestId': msg['requestId'],
                        'resultType': 'error', 'error': 'unsupported-by-auto-resume'})
        if msg.get('type') == 'broadcast' and msg.get('method') == 'thread-stream-state-changed':
            params = msg.get('params', {})
            if params.get('conversationId') == self.thread_id and params.get('hostId') == self.host and msg.get('sourceClientId') == self.owner:
                if msg.get('version') != PROTOCOL['thread-stream-state-changed']:
                    raise AppError('App state protocol version changed')
                self.state_generation += 1
                change = params.get('change', {})
                if change.get('type') == 'snapshot':
                    self.snapshot_value = change.get('conversationState')
                    self.revision = change.get('revision')
                elif change.get('type') == 'patches':
                    # Do not infer current state from a partial/missed patch stream.
                    self.snapshot_value = None
                else:
                    raise AppError('Unsupported app state change format')
        return msg

    def request(self, method, params, version, target=None, host=None):
        request_id = str(uuid.uuid4())
        msg = {'type': 'request', 'requestId': request_id, 'sourceClientId': self.client_id,
               'method': method, 'params': params, 'version': version,
               'timeoutMs': int(self.timeout * 1000)}
        if target:
            msg['targetClientId'] = target
        if host:
            msg['hostId'] = host
        self._send(msg)
        deadline = time.monotonic() + self.timeout
        while True:
            reply = self._read(deadline)
            if reply.get('type') != 'response' or reply.get('requestId') != request_id:
                continue
            if reply.get('resultType') != 'success':
                # Only emit safe error codes, never raw remote error strings.
                code = reply.get('error')
                if code == 'no-client-found':
                    raise ThreadUnavailable('The selected conversation is not connected to the app yet')
                raise AppError('The app rejected the request or the interface is incompatible; not retried automatically')
            if reply.get('method') != method:
                raise AppError('App response method mismatch')
            return reply

    def follow(self, enabled):
        self._send({'type': 'broadcast', 'method': 'thread-stream-following-changed',
                    'sourceClientId': self.client_id, 'targetClientIds': [self.owner], 'version': PROTOCOL['thread-stream-following-changed'],
                    'params': {'hostId': self.host, 'conversationId': self.thread_id, 'following': enabled}})

    def follower_request(self, method, params, target=None):
        """A thread-follower request, host-scoped for conversations on another host."""
        if self.host == 'local':
            return self.request(method, params, PROTOCOL[method], target)
        return self.request(method, params, PROTOCOL[method] + HOST_SCOPED_VERSION_OFFSET, target, self.host)

    def discover_owner(self):
        if self.host == 'local':
            reply = self.request('thread-owner-discovery', {'hostId': 'local', 'conversationId': self.thread_id},
                                 PROTOCOL['thread-owner-discovery'])
        else:
            # The App answers owner discovery only for its own host; a read-only
            # host-scoped request is routed to the owning window instead.
            reply = self.follower_request('thread-follower-read-model-settings', {'conversationId': self.thread_id})
        return reply.get('handledByClientId')

    def snapshot(self, thread_id):
        self.thread_id = str(uuid.UUID(thread_id))
        self.owner = self.discover_owner()
        if not isinstance(self.owner, str):
            raise AppError('Missing app task owner')
        self.follow(False)
        self.snapshot_value = None
        self.follow(True)
        deadline = time.monotonic() + self.timeout
        while self.snapshot_value is None:
            self._read(deadline)
        state = self.snapshot_value
        if not isinstance(state, dict) or state.get('id') != self.thread_id:
            raise AppError('The app returned a mismatched task state')
        return state

    def _drain_pending(self):
        """Consume already-arrived events before dispatch, never apply patches.

        This closes the queued-event window, not the inherent non-atomic gap
        between the App's state check and its start-turn operation. A busy or
        partial stream fails closed rather than allowing an unbounded drain.
        """
        deadline = time.monotonic() + self.timeout
        for _ in range(128):
            if not select.select([self.sock], [], [], 0)[0]:
                return
            self._read(deadline)
        raise AppError('App state is still changing; canceling this resume')

    def resume(self, thread_id, expected_fingerprint, message_id, dispatch_guard=None):
        check_version(self.app_path)  # Stop if the app updated while we were waiting.
        state = self.snapshot(thread_id)
        if decide(state).action != 'resume' or fingerprint(state) != expected_fingerprint:
            raise AppError('Task state changed before sending; canceling this resume')
        generation = self.state_generation
        self._drain_pending()
        if generation != self.state_generation:
            raise AppError('A new task state arrived before sending; canceling this resume')
        request = {'threadId': thread_id, 'clientUserMessageId': message_id,
                   'input': [{'type': 'text', 'text': CONTINUATION, 'text_elements': []}]}
        if dispatch_guard is not None and not dispatch_guard():
            raise AppError('Monitoring stopped or send authorization changed; canceling this resume')
        reply = self.follower_request('thread-follower-start-turn',
                                      {'conversationId': thread_id, 'turnStart': {'request': request}}, self.owner)
        try:
            turn_id = reply['result']['result']['turn']['id']
            if not isinstance(turn_id, str) or not turn_id:
                raise KeyError('id')
        except (KeyError, TypeError) as exc:
            raise AppError('Resume result uncertain; check in the app. It will not be resent automatically') from exc
        return turn_id
