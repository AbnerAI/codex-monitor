# Codex-only automatic iTerm2 indicator — 0.1.3

## Intended behavior

After opt-in setup, new interactive zsh shells keep using the usual `codex`
command. The launcher prepares a session-local iTerm2 counter and updates it
for the exact client thread and normalized `CODEX_HOME`: `1 monitor`,
`2 monitors`, `3 monitors`, or blank at zero. Existing registered watchers can
appear after resume without a new watcher or model turn. Claude Code, other
running shells, and shared iTerm2 profiles are outside this integration.

## Why 0.1.3 changes setup

The 0.1.1/0.1.2 launcher injected `-c` settings for shell environment and native
terminal titles. Actual Codex 0.166.0 startup logs confirmed that these settings
could exclude the shared daemon and select an in-process backend. A visible
counter and a readable on-disk thread history did not establish that the monitor
could notify that runtime. Earlier isolated tests did not prove this launch path.

The 0.1.3 launcher injects no CLI configuration flags. The original command and
arguments are preserved; there is no `--remote` transport change or replacement
of account, profile, model, authentication, or permission choices. Its lease
state stays in the parent launcher, rather than being injected into tool
environments. The former runtime-title setup and its claim of no Codex config
write are superseded by the explicit setup below.

## One-time setup

The core installer copies the service, CLI, and skill without editing Codex
configuration or restarting a running service. Automatic display also requires:

1. The user-enabled iTerm2 Python API with its default authentication.
2. The optional `iterm2>=2.30,<3` client in the dedicated `iterm-runtime`.
3. The managed zsh function installed using `scripts/install_iterm.py --install`.
4. An explicitly configured native Codex title for the actual home.

The zsh installer defaults to a read-only preview and preserves existing aliases,
functions, unrelated rc contents, permissions, and a backup. It resolves the
original executable with `whence -p`; it does not rewrite an account-selection
wrapper or reload existing shells. No iTerm2 restart or shared-profile layout
change is needed.

With the selected shared Codex daemon already running:

```bash
codex-monitor iterm configure-title --codex-home /ACTUAL/HOME
codex-monitor iterm configure-title --codex-home /ACTUAL/HOME --apply
```

The first command previews the proposed change. Explicit `--apply` writes only
`tui.terminal_title` through the official configuration API with a version
check. It persists
`["app-name","thread-id","app-name","thread-name","project-name"]` for subsequent
Codex launches using that home. The native title begins
`codex | <native thread ID> | codex`, followed by descriptive fields.

If the existing daemon or writable configuration layer is unavailable, setup
fails clearly. Codex's native title picker or a user-managed configuration TOML
edit is an alternative; the integration does not force a missing layer or
replace unrelated settings. This persistent Codex setting is separate from the
per-session iTerm2 status-bar layout. A matching package version alone does not
mean automatic display is configured.

## Ownership, layout, and counting

Before mutation, the launcher proves the launching terminal by comparing its
actual stdin/stdout TTY with read-only iTerm2 metadata and obtaining the exact
matching UUID. GUI focus and inherited environment IDs alone are insufficient.
Subsequent API reads target only that UUID, without enumerating other sessions.
The launcher publishes `user.codexMonitor` through its held terminal FD, so
moving focus does not retarget the count.

The typed API helper captures only the session's status-bar layout and enabled
state. Existing components remain; the counter is added only if absent. New
counters and counters without an explicit text color use green `#00CC66`;
custom colors are preserved. The bar is enabled during the Codex run. Normal
exit clears the value and restores the captured settings when they still match
the applied layout. User edits during the run are preserved.

A temporary startup title marks a new launch epoch so an earlier client's title
cannot establish the new client's identity. Codex itself then emits its native
identity. Both leading fixed `codex` markers are required; task/project text is
never searched for UUIDs. Codex 0.166.0 truncates `thread-id` to 29 characters
plus `...`. The prefix must resolve to exactly one full active registered thread
inside the client's authoritative normalized home. Missing, malformed, or
ambiguous identity leaves the label blank. Another profile cannot supply a count.

Home discovery uses the original CLI entry and its daemon-version endpoint,
then takes `codexHome` from `initialize`. A cold home probe may use a bounded
temporary stdio app-server for initialization only and clean it up before the
real CLI launches. It creates no thread, turn, or model request. It is never a
notification endpoint, nor a substitute for the real shared runtime. No
additional backend or automatic thread resume is used to make an in-process
thread deliverable.

The count loop runs from startup and polls existing registrations independently
of `watch`. Registering a watcher does not attach an indicator. Noninteractive,
remote, `--no-daemon`, non-TTY, non-iTerm2, and unsupported multi-pane launches
skip automatic decoration. Display failure still allows the original Codex
command to run. Closing the display does not stop watchers or the service.
A hard kill may leave stale display state until a later launch repairs it.

## Runtime readiness and upgrades

`doctor` combines `thread/read` identity with `thread/loaded/list` runtime
membership. Its `runtime_loaded`, `delivery_ready`, `registration_ready`, and
`delivery_state` fields distinguish history from a live owning runtime.
It prints structured JSON and exits 2 when registration is unavailable; an
eligible approval/input wait remains a successful diagnostic result.
New registrations fail before storage when loading cannot be verified, the
thread is unloaded, direct input is unsupported, or a system error is present.
Approval/input waits permit registration and retain pending delivery.

Existing watchers that later unload retain events as pending, without a TTL or
automatic drop. The monitor does not approve requests or resume threads.
The new CLI preflight also protects new registrations sent to an old running
service. Existing watchers need a planned service upgrade/restart to receive the
new backend guard. Installation does not restart a shared service or interrupt
other watchers automatically.

## Manual integration and validation

Manual `iterm attach/status/detach` remains an independent, explicit path. It
only publishes the private user variable, works with the Python API disabled,
and pauses when the authorized session is not current. It does not perform the
persistent native-title setup. Do not silently fall back to manual binding or
ask for UUID confirmation when inspecting automatic setup.

The 0.1.0 live component/variable checks and the 0.1.1/0.1.2 isolated results
remain historical evidence. All 183 isolated regressions passed in 0.1.3.
The real CLI/private-PTY check verified routing, notification acceptance, and a
canonical model acknowledgement in a rerun with user-task warmup, observed idle,
and a larger bounded deadline. The earlier empty-fixture timeout remains
recorded; these changes do not isolate its cause. The successful
rerun used direct `CodexRPC.deliver`, with no watchers or monitor service
created; it does not verify the live watcher-worker path. Actual GUI and
persisted title application remain unverified. Shared-runtime readiness and
visible counting need separate evidence; a layout check alone is insufficient.
Results are recorded in [validation.md](validation.md).
