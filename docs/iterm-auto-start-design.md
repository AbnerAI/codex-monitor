# Codex-only automatic iTerm2 indicator — 0.1.1

## Intended behavior

After a one-time opt-in setup, the user opens a new interactive zsh and types
`codex` as before. A counter component is prepared for that Codex run, without
manual component configuration in each new terminal. Two active watchers for
the exact conversation/profile show `2 monitors`; one shows `1 monitor`,
three show `3 monitors`, and zero is blank. Claude Code's
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
per-invocation monitor tool-environment settings and a client-local native
terminal-title format. Model and permission settings are not replaced. No
remote server, proxy, or extra model turn is introduced.

Before any layout change, the launcher compares the real stdin/stdout terminal
FD with read-only focused-session metadata and obtains the exact matching UUID.
Neither GUI focus alone nor a cached environment UUID is ownership proof.
A narrow typed API helper reads `Status Bar Layout`, `Show Status Bar`, and the
owned client's native identity for that explicit session. It does not obtain a session list
or operate on shared profiles. The captured layout is copied, existing
components retained, and one Interpolated String counter appended only if the
same expression is absent. The session's bar is enabled for the run. Shared
profiles and unrelated session properties are not changed.

For this invocation, the launcher passes the native Codex setting
`tui.terminal_title=["app-name","thread-id","app-name","thread-name","project-name"]`.
This changes the running Codex client's own native terminal title. The leading
fields must be exactly `codex | <native thread ID> | codex`; task/project names
are descriptive only. No Codex configuration file is written. A temporary
startup title prevents an earlier client's leftover title from binding the
new launch before the official client publishes its identity.

If safe capture or setup is unavailable, Codex still starts with a diagnostic;
the optional indicator cannot make the original command unusable. Automatic
setup requires the user-enabled Python API and is unavailable while that API
is disabled; the core/manual methods remain usable.

## Client-owned automatic count

The launcher owns the count loop from startup/reconnect, independent of
`watch`-time tool execution. Each tick reads native identity from the exact
owned UUID and active watcher scopes from the registry, then publishes
`user.codexMonitor` through the launcher's held TTY FD. Focus changes do not
retarget or pause this owned-terminal publishing path.

Codex 0.166.0 renders the `thread-id` item as 29 characters followed by `...`.
The parser accepts only the configured leading native fields, with both fixed
`codex` markers; it never searches arbitrary title text for UUIDs. That native
field is resolved only against active registry entries in the client's
authoritative `CODEX_HOME`. The launcher obtains that home through the original
CLI entry, retaining its account/home-selection wrapper, and normalizes it
before filtering. Other registry homes cannot establish ownership. Within that
exact home, exactly one full thread ID must match, including when a full UUID
is available. An unresolved home or multiple matching thread IDs hides the
count. Stopped or malformed registry entries cannot establish a scope.

The home probe uses the original CLI's daemon-version lookup to select its
endpoint and takes authoritative `codexHome` from `initialize`. If no daemon
exists on a cold launch, a bounded temporary stdio app-server supplies only that
initialization and is cleaned up afterward. It does not start a daemon, thread,
turn, or model request, or change configuration.

This restores a resumed conversation's existing watcher count without requiring
a new watcher, backend environment refresh, or model turn. It also avoids
cached environment overrides retained by resumed sessions. A private lease,
random token, PID, and heartbeat still protect launcher lifetime and stale-owner
cleanup; they are not a replacement for the client-owned terminal proof.

## Manual and legacy identity

For explicit manual operations, precedence is a user-confirmed `--session`, then
a valid scoped launch lease, then legacy `ITERM_SESSION_ID` only if all scoped
identity is absent. Incomplete, invalid, expired, or mismatched scoped identity
fails closed instead of falling back to stale backend environment. The default
manual path now honors a validated launch marker instead of ignoring it.

`codex-monitor iterm identify` is read-only. It reports full expected/observed
UUIDs, source, validation state, and token presence without revealing token
values. The observed focused UUID is diagnostic only. After a mismatch, only
the user's confirmation of the intended terminal authorizes an explicit
`--session`; no focus switch or environment copy authorizes a rebind.
An unconfirmed legacy candidate requires explicit `--session` confirmation to
create or rebind an attachment, even when GUI focus happens to match. An
existing matching attachment can be inspected without creating a new binding.

The `watch` CLI no longer invokes automatic attachment. The launcher loop owns
display updates, and manual attachment is an explicit operation. A display
failure is separate from a successful watcher registration and must not cause
duplicate registration. The manual AppleEvents updater still pauses when a
different terminal is current; it never selects or changes that other shell.

## Exit and failures

Normal exit invalidates the lease, stops only its own display updater, clears
the counter through the held terminal FD, and restores the captured bar layout/
enabled state if the current state still matches what the launcher applied.
User layout edits during the run are preserved. Closing the display does not
stop watchers or the shared monitor service.

`SIGKILL` prevents guaranteed immediate cleanup. A counter or blank bar may
remain after its client-owned updater is killed. The next Codex launch may
repair the stale layout when its current state still matches the previous
applied state. No stale owner may overwrite a newer launch or user layout edit.

## Upgrade and diagnostics

Release 0.1.1 requires the updated service/CLI and managed zsh hook. New
interactive shells should resolve the managed `codex` function while
`whence -p codex` still points at the untouched original executable. Existing
aliases/functions are preserved and may need explicit user integration. An
already-running client needs a new launch for its native identity markers;
other running shells must not be killed, closed, or reloaded as an upgrade step.

`undef` is an unresolved/uninitialized user variable, not zero. Blank may be
zero watchers or deliberately withheld unavailable/ambiguous identity. Inspect
the read-only identity diagnostics and current status before rebinding.

## Verification boundary

Deterministic tests cover shell argument forwarding and existing-command
preservation, opt-in installation/removal, narrow API requests, layout
preservation, ownership, leases, command exclusions, and failure/exit paths.
Core watcher and manual counter tests do not establish automatic layout support.
Live automatic acceptance additionally requires the user-enabled API and a
current-session-only capture/apply/readback/restore check. Opening, closing,
or probing unrelated running shells is outside that check. In 0.1.0, this
lifecycle check passed in the authorized current shell: component addition, exact-thread
binding, the then-current count label, exit clearing, and original layout restoration were
verified while the shared service instance remained unchanged. It did not open
a new iTerm2 window or launch a fresh interactive Codex TUI. That historical
check does not establish the 0.1.1 fresh/resumed-client fixes. Release-specific
results and test counts belong in [the validation record](validation.md).
