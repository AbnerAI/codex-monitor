# Validation record

## 0.1.3 shared-runtime correction

Actual Codex 0.166.0 startup logs confirmed that the launcher's injected `-c`
environment/title settings could exclude the shared daemon and start an
in-process thread. The 0.1.1/0.1.2 isolated and layout checks below did not prove
that a freshly launched client remained in the intended shared runtime.

Release 0.1.3 removes every injected `-c` flag and forwards the original CLI
arguments unchanged. Native title identity now requires an explicit one-time
persisted `tui.terminal_title` setup for the actual home. `iterm configure-title`
is a read-only preview; `--apply` writes only that key through the official
configuration API with a version check. It requires an existing shared daemon
and applies to subsequent CLI launches. Missing daemon/configuration layers
fail clearly; setup does not force a layer or alter unrelated settings.

`doctor` now combines exact `thread/read` identity with `thread/loaded/list`.
It reports `runtime_loaded`, `delivery_ready`, `registration_ready`, and
`delivery_state`. New registrations reject unloaded/unverified targets,
unsupported direct input, and system errors before storage. Approval/input
waits remain eligible for registration and temporarily defer delivery.
`doctor` emits structured JSON and exits 2 when `registration_ready` is false;
an eligible approval/input wait retains a successful exit status.
Existing watchers that later unload keep pending events without a TTL or
automatic drop. No backend or thread resume is used to repair runtime ownership.
A temporary stdio initialize may still be used solely to discover the original
wrapper's home before launch; it creates no thread or model turn and is never
the notification endpoint.

The CLI preflight protects new registrations even with an older running
service. Updating existing watchers' backend guards requires a separately
planned service upgrade/restart; no shared service or other shell is restarted
automatically. The 0.1.2 green-default behavior is retained.

| 0.1.3 check | Result |
|---|---|
| Isolated unit/integration regressions | 183 passed |
| Actual current-thread `doctor`, Codex 0.166.0 | Passed: exact shared-runtime thread loaded, registration and delivery ready |
| Actual `iterm configure-title` preview | Passed read-only against Codex 0.166.0; `applied: false` |
| Actual persisted title setup in the user's Codex home | Not applied during these checks |
| Official CLI/private-PTY shared-runtime routing | Verified: fixture was unloaded before CLI startup, then loaded and delivery-ready in the same backend after the CLI resumed its exact ID; original argv had no injected `-c` |
| Actual notification transport through `CodexRPC.deliver` | Accepted turn returned and canonical `functionCallOutput` observed; `notification_accepted: true` |
| First 90-second model-acknowledgement attempt | Timed out: turn remained running until fixture cleanup; no canonical agent item matched, `model_ack_verified: false`, overall report `passed: false` |
| Warmed user-task rerun with a larger bounded deadline, observed idle then acknowledgement | Passed: canonical READY user/agent items belonged to the same warmup turn; after observed idle, the canonical agent ACK matched the exact accepted notification turn; `warmup_ready`, `observed_idle_after_warmup`, `model_ack_verified`, and overall `passed` were true |
| Live watcher-worker path in this routing harness | Not exercised; delivery called `CodexRPC.deliver` directly, with zero watchers and no monitor service created; worker behavior has isolated coverage |
| Actual iTerm2 GUI end-to-end verification | Pending; GUI, terminal identity, and home lookups were mocked in the private-PTY harness |

The routing harness exercised the production launcher decoration path with the
official CLI. Only the iTerm2 GUI and its identity/home lookups were replaced.
It first established an unloaded baseline for its own known fixture, so a
previously loaded RPC-created thread could not make the routing check pass.
It then proved the resumed CLI belonged to the same shared runtime. No live
iTerm2 profile or native title configuration was written, no monitor service
was started, and no watchers were created. The successful rerun stopped its own
fixture client (exit 143) and archived its thread during cleanup.

The first empty-fixture attempt established routing and accepted external tool
output but did not verify an agent reply within 90 seconds. A subsequent rerun
used a larger bounded deadline and first established a faithful user task with canonical READY user and agent
items in its warmup turn, then observed the fixture idle. The real notification
was accepted afterward, and its canonical agent ACK belonged to that exact
accepted notification turn. This rerun reported `passed: true`,
`transport_verified: true`, `notification_accepted: true`, and
`model_ack_verified: true`; it does not erase the earlier timeout. Because both
task context and the waiting budget changed, these checks do not isolate what
caused the first attempt to time out.

The successful rerun verifies shared-runtime routing, native notification
acceptance, and a real model acknowledgement. It does not verify visible GUI
behavior, persisted title application, or the full file-watcher/worker delivery
path. The earlier actual current-thread idle acknowledgement remains separate
historical evidence below.

To opt in to the real CLI routing check, including real model delivery:

```bash
python3 scripts/routing_check.py --run --deliver \
  --codex-home /ACTUAL/HOME --codex-bin /ORIGINAL/CODEX \
  --cwd /TRUSTED/WORKSPACE --timeout 300 --output /PRIVATE/REPORT.json
```

`--run` is required and creates/archives only an owned exact fixture; it does
not infer the latest thread or alter workspace trust. Without `--deliver`,
startup verification submits no model turn. Explicit `--deliver` creates real
model turn(s) using the selected account and attempts to match a canonical agent
acknowledgement to the accepted turn. It can incur normal model usage. Inspect
`transport_verified`, `notification_accepted`, `warmup_ready`,
`observed_idle_after_warmup`, `model_ack_verified`, and overall `passed`
separately. A longer deadline alone is not evidence of success; the successful
rerun also verified the canonical warmup and notification-turn identities.

## 0.1.2 green default (historical)

All 155 regression tests passed. Launcher lifecycle coverage checks the native
green sRGB color dictionary, adding the default to an existing counter without
an explicit color, preserving custom colors and unrelated components, and
restoring the original layout on exit. No live iTerm2 color change was performed
for this release. New launches after upgrade apply the default; existing
terminals are not modified by installation.

## Earlier validation

Environment: macOS, Python 3.9.6, Codex CLI/app-server 0.166.0.

The following live GUI observations are historical 0.1.0 evidence. Their
recorded labels are preserved exactly; they are not new 0.1.1 GUI results.
The 0.1.1 regression checks are described separately below.

| Check | Result |
|---|---|
| Standard-library unit/integration tests, prior 0.1.0 run | 92 passed, including launcher, lease, restricted API bridge and zsh installer tests |
| Skill frontmatter validation | Passed |
| Source installer and installed skill wrapper | Passed |
| Real owning thread identity over Unix WebSocket | Passed |
| Real event delivery while the owning thread is active | Passed; external tool output observed and queried in history |
| Real file event while a test thread is idle | Passed; a new turn id was accepted and external tool output observed |
| Actual current-thread idle model acknowledgement | Passed; independent idle observation, accepted event turn, and canonical agent acknowledgement in that same thread and turn |
| Opt-in iTerm2 user-variable publication, historical hyphen format | Passed: blank → `monitor-2` → `monitor-1` → blank in the same exact session and thread; title format and service instance unchanged; both test-owned watchers stopped |
| CLI `statusline --show-zero --json` | Passed; label `monitor-0`, numeric `active: 0`, and `available: true` |
| Opt-in iTerm2 user-variable publication, original pre-hyphen format | Passed: blank → `monitor2` → `monitor1` → blank in the authorized current single-pane session; title override format remained unchanged |
| Visible iTerm2 status-bar indicator, original pre-hyphen format | Passed; user confirmed visible `monitor2` in the authorized current shell while two test-owned watchers were active |
| Original Codex entry through the new launcher | Passed for `--version`; original account-selection wrapper retained |
| Automatic layout lifecycle, real iTerm2 API | Passed in the authorized current shell: automatic component addition, exact thread binding, `monitor-2`, exit clearing and restoration of the original layout |
| Real `watch` command automatic binding | Passed; both real CLI registrations attached under the current launch lease and only their own two empty-file watchers were stopped |
| Release archive privacy scan | No account data, runtime state or personal installation paths |

The actual-session check used `scripts/session_check.py` on the conversation
identified by `CODEX_THREAD_ID`, without creating another model session or
overriding model, credentials, permissions, or Codex configuration. An independent
observer saw that thread remain idle for at least 0.5 seconds before appending a
fresh marker. The watcher accepted the event, and the same thread produced an
`agentMessage` containing the acknowledgement token. Its returned `turnId` matched
the accepted turn; the report also records the matched agent item ID. The local
report has `passed: true` and `agent_response_verified: true`. Tool output, user
echoes, and an RPC acknowledgement alone do not satisfy that criterion.

To repeat this opt-in check, start the monitor and run
`python3 scripts/session_check.py arm` from a shell tool in the intended active
Codex conversation. End the turn. When the monitor marker wakes it, let the agent
reply with the returned acknowledgement token, then inspect the report using
`python3 scripts/session_check.py report --directory /path/returned/by/arm`.
`confirm` with the same directory rechecks the canonical agent item and its turn
association without starting another model turn. Live checks use the current
Codex account and are separate from CI.

The manual iTerm2 integration leaves the official Codex CLI unchanged and publishes only
the private session variable `user.codexMonitor`. `iterm attach`, `iterm status`,
and `iterm detach` operate on the explicitly authorized current session. The
label counts active watchers for the exact thread and normalized `CODEX_HOME`.
Current labels are `1 monitor`, `2 monitors`, `3 monitors`, and blank at zero.
It does not
add a native Codex footer item, alter
titles or shared profiles, enable a global API, or operate on other shells.
Both the original pre-hyphen check and the historical hyphen-format live check
started from zero, registered two empty-file watchers owned by the tested
thread, then stopped both. The historical hyphen-format observations were blank →
`monitor-2` → `monitor-1` → blank. Each observation matched the current
`user.codexMonitor` value, the title format stayed unchanged, and the monitor
service instance stayed the same. The variable checks are separate from the
user-confirmed visual check described below.

To display the variable, use **Session > Edit Session > Session > Status bar >
Configure** in that session. Drag **Interpolated String** from the upper
**Status Bar Component Menu** to the lower **Active Components**, select it,
then use **Configure Component** to set `\(user.codexMonitor)` as its String Value.
This is a current-session setting; new tabs, panes, and sessions do not inherit
it. With manual setup, future sessions need a one-time component setup in a saved or dedicated
iTerm2 profile, used when opening those sessions. That persistent display setup
is separate from exact Codex thread binding: a new thread still needs an explicit
attach, and an existing attachment must be detached before rebinding. No
persistent shared-profile setup was performed by this manual path. The optional
automatic launcher is tested separately below.

Before the hyphen was added, during a separate
30-second visible check with two test-owned empty-file watchers active, the user
explicitly confirmed the original value `monitor2` in the authorized current shell. The
visibility evidence is the user's visual confirmation.

The optional automatic launcher has separate coverage for exact argument
forwarding, noninteractive skips, API-unavailable fallback, token ownership,
concurrent registration, typed layout capture, partial-apply rollback, user-edit
preservation, signal/exit handling and managed zsh installation/removal. The
optional SDK was installed in a dedicated runtime and its signatures checked.
The live API returns the checkbox as a numeric `0` or `1` on this installation;
the adapter normalizes only that scalar flag and preserves nested layout types.
The actual API lifecycle check is `python3 scripts/launch_check.py --output
/path/to/private-report.json`; it targets only the authorized current shell,
creates only its own empty-file watchers, and restores the user's layout.
After the user enabled the API, this check passed. It temporarily removed only
the project's counter component, automatically added it again, and invoked two
actual `watch` CLI registrations. Both used the exact launch lease, current
terminal, thread and selected Codex home. The observed label was `monitor-2`.
The test then stopped its own watchers and updater, verified exit clearing and
layout restoration, and restored the user's original layout and manual binding.
The shared service instance did not change. No other iTerm2 window or fresh
interactive Codex TUI was opened; the new-shell function and original command
forwarding have separate isolated coverage. The report has `passed: true` and
`real_watch_command_auto_binding: true`.

## 0.1.1 third-party regression follow-up

This section describes the historical implementation. Its runtime `-c`
title/environment injection and configuration-free setup were superseded by
0.1.3 after the shared-daemon exclusion was reproduced. The following isolated
checks are not evidence that the old launch path preserved runtime ownership.

The third-party review identified cross-terminal and resume risks in relying
on tool-inherited terminal IDs or waiting for a new `watch`. The replacement
parent/client count loop starts with the launcher, validates its real terminal
FD against read-only metadata, and reads native identity only from its owned
terminal. Its then-runtime-configured native title included the leading
`codex | <thread ID> | codex` markers. Codex 0.166.0's truncated ID prefix is
resolved only when the active watcher registry supplies exactly one full
thread ID plus normalized `CODEX_HOME` pair; ambiguity hides the count.
Existing resumed-thread watchers do not need to be registered again.

The client profile is established through the original CLI entry and an
initialize response, then used to filter the registry before ID resolution.
A sole matching watcher in another profile cannot supply this client's count.

Isolated mocked A/B-terminal and resume regression checks were run for this
change. They exercise stale inherited IDs, client-owned count resolution, and
existing watcher recovery; they did not change real GUI state or terminal
bindings. Current output grammar is `1 monitor`, `2 monitors`, `3 monitors`,
with zero blank. The earlier real 0.1.0 GUI observations above remain historical
evidence, not a claim that this new client loop has passed a fresh live GUI
run. The final 0.1.1 standard-library regression run passed all 154 tests,
including the native transport tests using temporary local Unix sockets.
