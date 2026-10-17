# Fork notes

This is a maintained fork of [progressrdx/codex-auto-resume](https://github.com/progressrdx/codex-auto-resume)
(remote `upstream`). It builds on upstream commit `22c773c`, audited 2026-10-14, with these changes:

- **Protocol check instead of a version pin.** `check_version()` reads the IPC method-version
  table from the App's `app.asar` and requires `PROTOCOL` in `codex_resume/app.py`
  (state-changed 11, following-changed 1, owner-discovery 1, follower-start-turn 2). Any App
  version declaring those versions is accepted. The same table supplies the versions the
  client sends.
- `codex_binary()` finds the CLI at `Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex`.
- 26.1013 dropped `ephemeral` and `queuedFollowUps` from the conversation state. A missing
  `ephemeral` is accepted only when the `rolloutPath` file exists. Queued follow-ups are now
  UI-only, so the tool can't see them.
- English UI, CLI and continuation prompt.

Verified live on 26.1007 and 26.1013: tests, `doctor`, `list`, `check`. A real resume across a
natural usage reset succeeded on 26.1007 (2026-10-16 01:33, app confirmed the new turn).

## Use

```bash
~/codex-auto-resume/resume list                 # find the thread UUID
~/codex-auto-resume/resume check <UUID>         # read-only; needs canMonitor: true
~/codex-auto-resume/resume start <UUID>         # watch; up to 3 resumes
~/codex-auto-resume/resume status
~/codex-auto-resume/resume stop <UUID>
```

Keep the ChatGPT app open. Watchers don't survive a reboot.

## After a ChatGPT app update

Usually nothing is needed. If `doctor` reports `App IPC protocol changed (...)`, the App changed
a message the tool sends or reads, so review the named method in the new bundle before updating
`PROTOCOL`. If `check` rejects a conversation it used to accept, the conversation state shape
changed; compare the live snapshot's keys against `policy.decide()`.

## English translation

The dashboard, CLI help, errors and status reasons are translated to English (commit "Translate
dashboard, CLI and status messages to English"). Upstream updates will conflict with these strings;
merge upstream and redo the translation where needed.

## Syncing upstream

```bash
git fetch upstream
git merge upstream/main     # resolve string conflicts in favor of English
python3 -m unittest discover -s tests && node --test tests/test_ui.cjs
git push
```

Review upstream changes before merging, as with the original audit: they run against your Codex app.

## Cloud (durable) conversations

Conversations on the App's cloud `durable` host ("Long-lived") are listed from
`~/.codex/.codex-global-state.json` and resumed through the App, like local ones:

- The App answers `thread-owner-discovery` only for its own host, but every `thread-follower-*`
  request is routed by the request's own `hostId`. Host-scoped requests use the table version + 1
  (`HOST_SCOPED_VERSION_OFFSET`). `thread-follower-read-model-settings` (read-only) finds the
  owning window; state follows and `thread-follower-start-turn` then carry `hostId: durable`.
- Durable state has no `ephemeral` and an empty `rolloutPath`; the host itself marks it persisted.
- Usage-limit failures carry the same `codexErrorInfo: usageLimitExceeded` and use the same
  `codex` usage bucket (the cloud error message prints the reset time in UTC).
- If the conversation isn't loaded in the App, `check` reports it as connecting and the watcher
  opens `codex://threads/<id>?hostId=durable`.

Verified read-only on 26.1013 (2026-10-17): owner routing, live snapshot, `check`. A real cloud
send has not run yet.

Ruled out: the `durable` app server at `wss://codex-cloud-backend.chatgpt.com:443` returns 403 to
both a stdlib client (Cloudflare challenge) and `codex agents --remote`; `codex cloud list`
doesn't include these conversations.
