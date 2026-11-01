# Validation record

Environment: macOS, Python 3.9.6, Codex CLI/app-server 0.166.0.

| Check | Result |
|---|---|
| Standard-library unit/integration tests | 92 passed, including launcher, lease, restricted API bridge and zsh installer tests |
| Skill frontmatter validation | Passed |
| Source installer and installed skill wrapper | Passed |
| Real owning thread identity over Unix WebSocket | Passed |
| Real event delivery while the owning thread is active | Passed; external tool output observed and queried in history |
| Real file event while a test thread is idle | Passed; a new turn id was accepted and external tool output observed |
| Actual current-thread idle model acknowledgement | Passed; independent idle observation, accepted event turn, and canonical agent acknowledgement in that same thread and turn |
| Opt-in iTerm2 user-variable publication, current `monitor-N` format | Passed: blank → `monitor-2` → `monitor-1` → blank in the same exact session and thread; title format and service instance unchanged; both test-owned watchers stopped |
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
label counts active watchers for the exact thread and normalized `CODEX_HOME`:
`monitor-N` (for example, `monitor-2` or `monitor-1`), or blank at zero. It does not
add a native Codex footer item, alter
titles or shared profiles, enable a global API, or operate on other shells.
Both the original pre-hyphen check and the current-format live count check
started from zero, registered two empty-file watchers owned by the tested
thread, then stopped both. The current-format observations were blank →
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
