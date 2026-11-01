# Codex-only automatic iTerm2 indicator

## Intended behavior

After a one-time opt-in setup, the user opens a new interactive zsh and types
`codex` as before. A counter component is prepared for that Codex run, without
manual component configuration in each new terminal. Two active watchers for
the exact conversation/profile show `monitor-2`; zero is blank. Claude Code's
entry point and settings remain untouched. Existing shells are not reloaded,
closed, or restarted, and shared iTerm2 profiles are not edited.

The existing manual indicator remains supported independently. It works with
the Python API disabled and uses only the standard-library service plus a
guarded current-session user variable.

## Installation boundary

The core installer copies the service and skill without changing Codex
configuration. Automatic layout capture uses a separate optional
`iterm2>=2.30,<3` client, installed in
`~/.local/share/codex-monitor/iterm-runtime`. The user must enable iTerm2's
**Settings > General > Magic > Enable Python API** once. Neither the service nor
skill toggles that global switch automatically. Default API authentication is
retained; `AllowAllApps` is not required. Enabling the API does not require
restarting iTerm2 or closing existing shells. Automatic setup does not require
editing a saved/shared profile's status-bar settings.

`codex-monitor iterm api-status` is a read-only diagnostic for the explicitly
authorized current UUID and single-pane tab. It reads only the two relevant
session properties and reports `api_available: true` on success. It does not
register watchers, enumerate terminals, or change settings.

`scripts/install_iterm.py` defaults to a read-only preview of its managed zsh
block. `--install` appends/replaces only that block in `~/.zshrc`, preserving
other bytes, permissions, and a backup. `--uninstall` removes only the block.
New interactive shells define a `codex` function unless the user already has a
function or alias with that name. It resolves the original executable through
`whence -p`, passes arguments without evaluation, and falls back to that
executable if the monitor is unavailable. The original executable or account/
`CODEX_HOME` selection wrapper is not rewritten.

Installing the function does not source any existing shell. Uninstalling it
does not terminate running Codex processes, disable the API, or remove the
optional runtime.

## Launch and session layout

`codex-monitor launch --codex ORIGINAL -- ARGUMENTS` supervises the original
command in its existing foreground terminal. Decoration applies only to local
interactive Codex TUI launches in iTerm2 with matching stdin/stdout terminals.
Noninteractive commands, help/version, remote/no-daemon launches, non-TTY
calls, and other terminals skip decoration. The current adapter requires a
single-pane tab. The original argument sequence is preserved, apart from
per-invocation settings for monitor-specific tool
environment variables; model and permission settings are not replaced.

Before any layout change, the launcher checks the expected current iTerm2 UUID
and terminal FD. A narrow typed API helper reads only `Status Bar Layout` and
`Show Status Bar` for that explicit session. It does not obtain a session list
or operate on shared profiles. The captured layout is copied, existing
components retained, and one Interpolated String counter appended only if the
same expression is absent. The session's bar is enabled for the run. Other
session properties and terminal titles are not changed.

If safe capture or setup is unavailable, Codex still starts with a diagnostic;
the optional indicator cannot make the original command unusable. Automatic
setup requires the user-enabled Python API and is unavailable while that API
is disabled; the core/manual methods remain usable.

## Exact conversation binding

A private lease binds a random launch token to the terminal UUID, launch PID,
and heartbeat. Monitor-specific token, session, and state-directory variables
are supplied to this Codex invocation's tool environment. This avoids treating
an environment inherited earlier by a shared app-server as terminal identity.

When a watcher is registered from that conversation, `watch` can attach the
counter only after validating the live lease. Its count uses the exact Codex
thread and normalized active profile selected by the original launcher. It
does not infer a new thread from a title, directory, modification time, or
another session. Without a valid token, normal watching remains available but
automatic terminal binding is skipped. A display failure is returned separately
from the successfully armed watcher and must not cause duplicate registration.

The count updater changes only `user.codexMonitor` in its authorized terminal.
If a different session is current, it pauses rather than selecting or changing
that session. The count may lag while paused. A new valid launch cannot be
cleared by a stale lease or updater.

## Exit and failures

Normal exit invalidates the lease, stops only its own display updater, clears
the counter through the held terminal FD, and restores the captured bar layout/
enabled state if the current state still matches what the launcher applied.
User layout edits during the run are preserved. Closing the display does not
stop watchers or the shared monitor service.

`SIGKILL` prevents guaranteed immediate cleanup. A lease-bound updater can clear
the counter after detecting an expired owner, but may need to wait until that
terminal becomes current. A blank bar may remain. The next Codex launch may
repair the stale layout when its current state still matches the previous
applied state. No stale owner may overwrite a newer launch or user layout edit.

## Verification boundary

Deterministic tests cover shell argument forwarding and existing-command
preservation, opt-in installation/removal, narrow API requests, layout
preservation, ownership, leases, command exclusions, and failure/exit paths.
Core watcher and manual counter tests do not establish automatic layout support.
Live automatic acceptance additionally requires the user-enabled API and a
current-session-only capture/apply/readback/restore check. Opening, closing,
or probing unrelated running shells is outside that check. This lifecycle check
has passed in the authorized current shell: component addition, exact-thread
binding, `monitor-2`, exit clearing, and original layout restoration were
verified while the shared service instance remained unchanged. It did not open
a new iTerm2 window or launch a fresh interactive Codex TUI. Current results
and test counts belong in [the validation record](validation.md).
