# Local notes (branch `local-chatgpt-26.1007`)

Audited upstream commit `22c773c` (2026-10-14), plus one local patch commit:

- `SUPPORTED_APP` pinned to ChatGPT.app `26.1007.71326` / build `20398`.
- `codex_binary()` finds the CLI at `Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex`.
- Continuation prompt translated to English.

Verified on 26.1007: all 99 tests, `doctor`, live `check`, and the app bundle's IPC version table
(state-changed 11, owner-discovery 1, following-changed 1, follower-start-turn 2) plus optional
`turnStart.context`. A real send has not yet run on this version.

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

`doctor` will refuse the new version. Check the new bundle's IPC version table matches the values
above, then bump `SUPPORTED_APP` in `codex_resume/app.py`, run the tests, `doctor` and `check`.
