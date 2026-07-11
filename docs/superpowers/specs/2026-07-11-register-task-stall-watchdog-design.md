# Register Task Stall Watchdog Design

## Goal

Prevent registration tasks from occupying a supervisor slot indefinitely when the browser automation subprocess remains alive but stops making progress.

## Context

Task 31 demonstrated the failure mode: its console log stopped changing shortly after round 16 began, while the subprocess remained alive for approximately six hours. The supervisor only finalized tasks after `process.poll()` returned an exit code, so a blocked DrissionPage/CDP call kept the task in `running` and prevented the next queued task from starting.

The current `last_log_at` value does not represent real activity because `parse_console_state()` assigns the current time on every supervisor poll. This masks stalled tasks and prevents reliable inactivity detection.

The current standalone Docker build imports `grok_helper/register.py`. The older integrated application still imports `app/products/web/admin/register.py` and has existing regression coverage, so the watchdog behavior must remain consistent in both modules.

## Requirements

- Detect a managed registration subprocess that is still alive but has produced no new console-log activity for a configurable interval.
- Default the inactivity interval to 300 seconds.
- Allow operators to override it with `GROK_REGISTER_CONSOLE_STALL_TIMEOUT`.
- Base activity on the console file's actual modification time rather than the supervisor polling time.
- Automatically terminate the entire subprocess process group when the inactivity threshold is exceeded.
- Mark a timed-out task `partial` when at least one registration succeeded, otherwise mark it `failed`.
- Record a clear terminal phase and error message explaining the inactivity timeout.
- Clear the task PID and set `finished_at` and `exit_code` when watchdog termination completes.
- Preserve normal completion, manual stop, application shutdown, and startup orphan-recovery behavior.
- Stop the per-task Xvfb display during normal runner shutdown so completed tasks do not leak display processes.
- Add regression tests before implementation.

## Approaches Considered

### Supervisor inactivity watchdog — selected

The supervisor already owns subprocess lifecycle and polls each managed task. It can compare the current time with the console file modification time before deciding whether the process remains healthy. This protects every blocking point in the registration runner without depending on the blocked Python thread regaining control.

This approach is localized, testable, and also handles future browser-library hangs outside the currently observed profile-submit path.

### Per-browser-call thread timeout — rejected

Wrapping DrissionPage calls in worker threads would not reliably stop a thread blocked in WebSocket or browser-library code. Timed-out threads could continue mutating the browser in the background and corrupt subsequent rounds.

### One subprocess per registration round — deferred

Process isolation per round would provide the strongest timeout boundary, but it requires a larger runner protocol and result-aggregation redesign. The supervisor watchdog provides the required recovery behavior without that refactor.

## Backend Design

### Configuration

Both register modules define `STALL_TIMEOUT_SECONDS` from `GROK_REGISTER_CONSOLE_STALL_TIMEOUT`, defaulting to `300`. The value is clamped to a positive minimum so an invalidly small value cannot cause immediate task termination.

The standalone Compose environment and example environment file expose the setting for operators.

### Real log activity time

`parse_console_state()` derives `last_log_at` from `console_path.stat().st_mtime` when the file exists. If the file does not yet exist, it returns an empty activity timestamp instead of the current poll time.

The supervisor resolves the effective activity time in this order:

1. Console file modification time.
2. The task's existing `last_log_at` value, when valid.
3. The task's `started_at` value.

This gives newly launched tasks the full timeout interval while ensuring repeated polling does not manufacture activity.

### Watchdog decision

During `_refresh_running()`, the supervisor first parses and persists the current console state, then polls the subprocess.

- If the subprocess has exited, existing finalization logic runs unchanged.
- If the subprocess is alive and the inactivity duration is below the threshold, it remains running.
- If the subprocess is alive and the inactivity duration meets or exceeds the threshold, the supervisor terminates the process group and waits for it to exit.

The timeout terminal state is independent of the signal-based manual-stop rule:

- `partial` when `completed_count > 0`.
- `failed` when `completed_count == 0`.

The supervisor writes:

- `current_phase = "stalled_timeout"`
- a Chinese `last_error` containing the configured number of seconds
- `finished_at`
- the observed exit code
- `pid = NULL`

The managed process and log handle are then removed and closed exactly once.

### Runner cleanup

The registration runner adds a small `stop_virtual_display()` helper. Its outer `finally` block closes the browser first and then stops the `_virtual_display` instance when one was started.

Supervisor termination already signals the process group, so stalled tasks also terminate Chrome and Xvfb descendants. Explicit display cleanup addresses the normal-completion leak observed in the running container.

## Testing Design

Tests will cover the authoritative standalone module and the legacy integrated copy:

- `parse_console_state()` reports the console file modification time and does not advance it when the file is unchanged.
- A live managed process with stale log activity is terminated and finalized as `failed` when no registrations succeeded.
- A stale task with successful registrations is finalized as `partial`.
- A live task with recent log activity is not terminated.
- Watchdog finalization clears the PID, records the timeout phase/error, closes the log handle, and removes the managed process.
- Environment loading accepts the default and an override value.
- The runner source includes normal Xvfb shutdown after browser shutdown.

Existing register-console and standalone-service suites must continue to pass. No live registration or external network call is required for the regression tests.

## Scope

In scope:

- Supervisor inactivity detection and terminal state handling.
- Accurate `last_log_at` persistence.
- Watchdog configuration exposure.
- Normal Xvfb cleanup.
- Regression tests and documentation.

Out of scope:

- Refactoring the two register modules into a shared package.
- Changing registration log formats beyond watchdog terminal messages.
- Retrying a timed-out registration round automatically.
- Deploying or restarting the running service after the code commit.
