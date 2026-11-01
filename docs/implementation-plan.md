# Implementation plan

1. Build the dependency-free CLI, durable SQLite store and bounded sources.
2. Connect an exact thread through the existing Codex Unix WebSocket; preserve
   monitor events as standalone tool output and respect human-input waits.
3. Add a private local control service with start/stop/list/unwatch/events/doctor.
4. Supply an installable skill, a source installer, release packaging, English
   and Chinese documentation, license and CI.
5. Run deterministic unit/integration tests, then real Codex daemon checks.
6. Install the validated local skill and service, package clean public artifacts,
   and audit the existing Claude resources without modifying them.
7. Add an opt-in per-session iTerm2 counter using a private user variable and
   manually configured status bar component; preserve the official Codex CLI.
