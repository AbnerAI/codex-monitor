# codex-monitor design

Approved scope: persistent file/command watchers, a Codex skill, and a portable
standalone repository. Reference: AbnerAI/dsh-monitor; this implementation uses
Python's standard library instead of DeepSeek Harness/Cordis APIs.

The CLI controls a private Unix-socket service. Source workers save checkpoints
and an event outbox in one SQLite transaction. Each watcher binds an exact Codex
thread UUID, CODEX_HOME, CLI executable and optional control socket. There is no
cwd-based or most-recent-session routing.

Delivery connects over the existing shared server's private Unix WebSocket.
`turn/start` with empty input and standalone `toolOutput` carries external source
data. No model, cwd, permission or approval overrides are supplied. Waiting-for-
approval/input and unloaded threads retain their pending events. The service
does not resume a thread or approve a request on the user's behalf.

An acknowledgement records accepted events and their turn id; it does not mean
the assistant processed them. A lost acknowledgement becomes uncertain rather
than causing an automatic duplicate submission. The user can inspect history
and explicitly retry. Stopping a watcher cancels unsent events; shutting down
the service preserves active watcher definitions for restart.

File sources begin at EOF by default, preserve incomplete UTF-8/NDJSON records,
and reset on inode replacement or truncation. Command sources run one poll at a
time, enforce time/output bounds, and report changed errors as well as output.
Commands run with the monitor service user's OS privileges; only explicitly
authorized commands belong in a watcher. Notification delivery preserves the
target Codex thread's own execution/approval settings.

Runtime data is private and separate from all Claude installations. Source and
package archives contain no account state, machine paths, or secrets.

## Validation

Test file partial records, Unicode, rotation/truncation, command change/error/
timeout/cancellation, restart recovery, uncertain submissions, stopping, and
exact profile/thread binding. Integration checks must use a real Codex daemon:
verify busy-turn delivery and a separate minimal thread's idle wake-up, then
inspect persisted output for an event marker and the resulting agent response.

