# Register Task Stall Watchdog Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Automatically terminate registration subprocesses that remain alive without producing log progress, persist truthful activity timestamps, and clean up Xvfb on normal runner exit.

**Architecture:** Keep lifecycle ownership in the existing `TaskSupervisor`. Derive activity from the console file modification time, compare it with a configurable 300-second threshold, and finalize stalled processes explicitly as `partial` or `failed` instead of letting signal exit codes turn them into manual `stopped` tasks. Mirror the supervisor behavior in the standalone and legacy modules, then add explicit Xvfb shutdown in the runner.

**Tech Stack:** Python 3.12, `unittest`, SQLite, `subprocess.Popen`, filesystem mtimes, FastAPI register modules, Docker Compose environment configuration.

---

## File Map

- `grok_helper/register.py`: authoritative standalone supervisor used by the current Docker build.
- `app/products/web/admin/register.py`: legacy integrated supervisor kept behaviorally consistent.
- `tests/test_grok_helper_standalone.py`: standalone configuration, activity timestamp, and watchdog regression tests.
- `tests/test_register_console.py`: legacy supervisor regression tests and recent-task non-termination coverage.
- `DrissionPage_example.py`: normal Xvfb lifecycle cleanup.
- `.env.example`: operator-facing default inactivity timeout.
- `docker-compose.yml`: pass the inactivity timeout into the running service.

### Task 1: Truthful Log Activity Timestamps

**Files:**
- Modify: `tests/test_register_console.py`
- Modify: `tests/test_grok_helper_standalone.py`
- Modify: `app/products/web/admin/register.py:399-453`
- Modify: `grok_helper/register.py:400-454`

- [ ] **Step 1: Add failing legacy timestamp test**

Add this test beside `test_parse_console_state_extracts_progress` in `tests/test_register_console.py`:

```python
def test_parse_console_state_uses_file_mtime_for_last_log_at(self):
    from app.products.web.admin.register import parse_console_state

    with tempfile.TemporaryDirectory() as tmp:
        log_path = Path(tmp) / "console.log"
        log_path.write_text("[*] 开始第 1 轮注册\n", encoding="utf-8")
        mtime = datetime(2026, 7, 11, 1, 27, 8).timestamp()
        os.utime(log_path, (mtime, mtime))

        first = parse_console_state(log_path)
        second = parse_console_state(log_path)

    expected = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")
    self.assertEqual(first["last_log_at"], expected)
    self.assertEqual(second["last_log_at"], expected)
```

Add `from datetime import datetime` to the test imports.

- [ ] **Step 2: Add failing standalone timestamp test**

Add the equivalent test to `tests/test_grok_helper_standalone.py`:

```python
def test_parse_console_state_uses_file_mtime_for_last_log_at(self):
    register = import_fresh_register()

    with tempfile.TemporaryDirectory() as tmp:
        log_path = Path(tmp) / "console.log"
        log_path.write_text("[*] 开始第 1 轮注册\n", encoding="utf-8")
        mtime = datetime(2026, 7, 11, 1, 27, 8).timestamp()
        os.utime(log_path, (mtime, mtime))

        state = register.parse_console_state(log_path)

    self.assertEqual(
        state["last_log_at"],
        datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S"),
    )
```

Add `from datetime import datetime` to the standalone test imports.

- [ ] **Step 3: Run both tests and verify RED**

Run:

```bash
python3 -m unittest \
  tests.test_register_console.RegisterConsoleHelperTests.test_parse_console_state_uses_file_mtime_for_last_log_at \
  tests.test_grok_helper_standalone.StandaloneRegisterServiceTests.test_parse_console_state_uses_file_mtime_for_last_log_at -v
```

Expected: both tests fail because `last_log_at` is set to the current poll time.

- [ ] **Step 4: Implement file-mtime timestamps in both modules**

Replace the initial `last_log_at` value in each `parse_console_state()` with an empty string, then populate it after the existence check:

```python
def _file_mtime_iso(path: Path) -> str:
    try:
        modified_at = path.stat().st_mtime
    except OSError:
        return ""
    return datetime.fromtimestamp(modified_at).strftime("%Y-%m-%d %H:%M:%S")


def parse_console_state(console_path: Path) -> dict[str, Any]:
    state = {
        "completed_count": 0,
        "failed_count": 0,
        "current_round": 0,
        "current_phase": "",
        "last_email": "",
        "last_error": "",
        "last_log_at": "",
    }
    if not console_path.exists():
        return state

    state["last_log_at"] = _file_mtime_iso(console_path)
```

In `_refresh_running()`, preserve the existing activity baseline when no file timestamp is available:

```python
parsed["last_log_at"] = (
    parsed["last_log_at"]
    or row["last_log_at"]
    or row["started_at"]
    or row["created_at"]
    or ""
)
```

When log parsing raises, set `last_log_at` to `row["last_log_at"] or row["started_at"] or row["created_at"] or ""` rather than `now_iso()`.

- [ ] **Step 5: Run both tests and verify GREEN**

Run the command from Step 3.

Expected: both tests pass.

- [ ] **Step 6: Commit truthful timestamp behavior**

```bash
git add -- app/products/web/admin/register.py grok_helper/register.py tests/test_register_console.py tests/test_grok_helper_standalone.py
git commit -m "fix: persist real register log activity"
```

### Task 2: Supervisor Stall Watchdog

**Files:**
- Modify: `tests/test_register_console.py`
- Modify: `tests/test_grok_helper_standalone.py`
- Modify: `app/products/web/admin/register.py:20-40, 765-855`
- Modify: `grok_helper/register.py:20-41, 766-856`

- [ ] **Step 1: Add failing legacy watchdog tests**

Add imports `from datetime import datetime, timedelta` and ensure `Mock` is imported. Add a helper inside the test class that creates a running task with a real console file and managed mock process:

```python
def _make_running_task(self, register, root: Path, completed_count: int, log_age_seconds: int):
    task_dir = root / "tasks" / "task_1"
    console_path = task_dir / "console.log"
    task_dir.mkdir(parents=True)
    lines = ["[*] 开始第 1 轮注册"]
    lines.extend(
        f"注册成功 | email=user{index}@example.com | password=x"
        for index in range(completed_count)
    )
    console_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    activity = datetime.now() - timedelta(seconds=log_age_seconds)
    os.utime(console_path, (activity.timestamp(), activity.timestamp()))
    log_handle = console_path.open("a", encoding="utf-8")
    process = Mock(pid=4321)
    process.poll.return_value = None
    process.wait.return_value = -15
    task_id = register.execute(
        """
        INSERT INTO tasks (
            name, status, target_count, completed_count, config_json,
            task_dir, console_path, pid, created_at, started_at, last_log_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "batch",
            register.STATUS_RUNNING,
            10,
            completed_count,
            json.dumps({"run": {"count": 10}}),
            str(task_dir),
            str(console_path),
            4321,
            register.now_iso(),
            register.now_iso(),
            activity.strftime("%Y-%m-%d %H:%M:%S"),
        ),
    )
    supervisor = register.TaskSupervisor()
    supervisor._processes[task_id] = register.ManagedProcess(task_id, process, log_handle)
    return task_id, supervisor, process, log_handle
```

Add three tests using temporary DB paths and `patch.object(register, "STALL_TIMEOUT_SECONDS", 60)`:

```python
def test_refresh_running_marks_stalled_task_failed(self):
    import app.products.web.admin.register as register

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "register"
        with (
            patch.object(register, "REGISTER_ROOT", root),
            patch.object(register, "TASKS_DIR", root / "tasks"),
            patch.object(register, "DB_PATH", root / "console.db"),
            patch.object(register, "STALL_TIMEOUT_SECONDS", 60),
        ):
            register.init_db()
            task_id, supervisor, process, log_handle = self._make_running_task(
                register, root, completed_count=0, log_age_seconds=120
            )
            with patch.object(register.os, "killpg") as killpg:
                supervisor._refresh_running()
            row = register.task_row(task_id)

    self.assertEqual(row["status"], register.STATUS_FAILED)
    self.assertEqual(row["current_phase"], "stalled_timeout")
    self.assertIn("连续 60 秒无日志进展", row["last_error"])
    self.assertIsNone(row["pid"])
    self.assertEqual(row["exit_code"], -15)
    self.assertEqual(supervisor._processes, {})
    self.assertTrue(log_handle.closed)
    killpg.assert_called_once_with(4321, register.signal.SIGTERM)

def test_refresh_running_marks_stalled_task_partial_when_progress_exists(self):
    import app.products.web.admin.register as register

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "register"
        with (
            patch.object(register, "REGISTER_ROOT", root),
            patch.object(register, "TASKS_DIR", root / "tasks"),
            patch.object(register, "DB_PATH", root / "console.db"),
            patch.object(register, "STALL_TIMEOUT_SECONDS", 60),
        ):
            register.init_db()
            task_id, supervisor, process, log_handle = self._make_running_task(
                register, root, completed_count=2, log_age_seconds=120
            )
            with patch.object(register.os, "killpg"):
                supervisor._refresh_running()
            row = register.task_row(task_id)

    self.assertEqual(row["status"], register.STATUS_PARTIAL)
    self.assertEqual(row["completed_count"], 2)
    self.assertEqual(row["current_phase"], "stalled_timeout")
    self.assertEqual(supervisor._processes, {})
    self.assertTrue(log_handle.closed)

def test_refresh_running_keeps_recent_task_running(self):
    import app.products.web.admin.register as register

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "register"
        with (
            patch.object(register, "REGISTER_ROOT", root),
            patch.object(register, "TASKS_DIR", root / "tasks"),
            patch.object(register, "DB_PATH", root / "console.db"),
            patch.object(register, "STALL_TIMEOUT_SECONDS", 60),
        ):
            register.init_db()
            task_id, supervisor, process, log_handle = self._make_running_task(
                register, root, completed_count=0, log_age_seconds=5
            )
            with patch.object(register.os, "killpg") as killpg:
                supervisor._refresh_running()
            row = register.task_row(task_id)

            self.assertEqual(row["status"], register.STATUS_RUNNING)
            self.assertIn(task_id, supervisor._processes)
            self.assertFalse(log_handle.closed)
            killpg.assert_not_called()
            log_handle.close()
            supervisor._processes.clear()
```

Use these concrete assertions for the failed case:

```python
self.assertEqual(row["status"], register.STATUS_FAILED)
self.assertEqual(row["current_phase"], "stalled_timeout")
self.assertIn("连续 60 秒无日志进展", row["last_error"])
self.assertIsNone(row["pid"])
self.assertEqual(row["exit_code"], -15)
self.assertEqual(supervisor._processes, {})
self.assertTrue(log_handle.closed)
killpg.assert_called_once_with(4321, register.signal.SIGTERM)
```

- [ ] **Step 2: Add failing standalone configuration and watchdog tests**

Add `Mock` to the standalone test imports. Add configuration coverage:

```python
def test_stall_timeout_defaults_to_300_seconds(self):
    register = import_fresh_register()
    self.assertEqual(register.STALL_TIMEOUT_SECONDS, 300)

def test_stall_timeout_can_be_overridden(self):
    register = import_fresh_register({"GROK_REGISTER_CONSOLE_STALL_TIMEOUT": "90"})
    self.assertEqual(register.STALL_TIMEOUT_SECONDS, 90)
```

Add this standalone helper and watchdog test to the class:

```python
def _make_running_task(self, register, root: Path, completed_count: int, log_age_seconds: int):
    task_dir = root / "tasks" / "task_1"
    console_path = task_dir / "console.log"
    task_dir.mkdir(parents=True)
    lines = ["[*] 开始第 1 轮注册"]
    lines.extend(
        f"注册成功 | email=user{index}@example.com | password=x"
        for index in range(completed_count)
    )
    console_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    activity = datetime.now() - timedelta(seconds=log_age_seconds)
    os.utime(console_path, (activity.timestamp(), activity.timestamp()))
    log_handle = console_path.open("a", encoding="utf-8")
    process = Mock(pid=4321)
    process.poll.return_value = None
    process.wait.return_value = -15
    task_id = register.execute(
        """
        INSERT INTO tasks (
            name, status, target_count, completed_count, config_json,
            task_dir, console_path, pid, created_at, started_at, last_log_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "batch",
            register.STATUS_RUNNING,
            10,
            completed_count,
            json.dumps({"run": {"count": 10}}),
            str(task_dir),
            str(console_path),
            4321,
            register.now_iso(),
            register.now_iso(),
            activity.strftime("%Y-%m-%d %H:%M:%S"),
        ),
    )
    supervisor = register.TaskSupervisor()
    supervisor._processes[task_id] = register.ManagedProcess(task_id, process, log_handle)
    return task_id, supervisor, process, log_handle

def test_refresh_running_marks_stalled_task_partial(self):
    register = import_fresh_register()

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "register"
        with (
            patch.object(register, "REGISTER_ROOT", root),
            patch.object(register, "TASKS_DIR", root / "tasks"),
            patch.object(register, "DB_PATH", root / "console.db"),
            patch.object(register, "STALL_TIMEOUT_SECONDS", 60),
        ):
            register.init_db()
            task_id, supervisor, process, log_handle = self._make_running_task(
                register, root, completed_count=2, log_age_seconds=120
            )
            with patch.object(register.os, "killpg") as killpg:
                supervisor._refresh_running()
            row = register.task_row(task_id)

    self.assertEqual(row["status"], register.STATUS_PARTIAL)
    self.assertEqual(row["completed_count"], 2)
    self.assertEqual(row["current_phase"], "stalled_timeout")
    self.assertIsNone(row["pid"])
    self.assertEqual(row["exit_code"], -15)
    self.assertEqual(supervisor._processes, {})
    self.assertTrue(log_handle.closed)
    killpg.assert_called_once_with(4321, register.signal.SIGTERM)
```

Add `timedelta` and `Mock` to the standalone test imports.

- [ ] **Step 3: Run watchdog tests and verify RED**

Run:

```bash
python3 -m unittest \
  tests.test_register_console.RegisterConsoleHelperTests.test_refresh_running_marks_stalled_task_failed \
  tests.test_register_console.RegisterConsoleHelperTests.test_refresh_running_marks_stalled_task_partial_when_progress_exists \
  tests.test_register_console.RegisterConsoleHelperTests.test_refresh_running_keeps_recent_task_running \
  tests.test_grok_helper_standalone.StandaloneRegisterServiceTests.test_stall_timeout_defaults_to_300_seconds \
  tests.test_grok_helper_standalone.StandaloneRegisterServiceTests.test_stall_timeout_can_be_overridden \
  tests.test_grok_helper_standalone.StandaloneRegisterServiceTests.test_refresh_running_marks_stalled_task_partial -v
```

Expected: tests fail because the timeout constant and watchdog terminal behavior do not exist.

- [ ] **Step 4: Add robust timeout configuration in both modules**

Add this helper beside the supervisor interval configuration:

```python
def _env_seconds(name: str, default: int, minimum: int = 30) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError:
        value = default
    return max(minimum, value)


STALL_TIMEOUT_SECONDS = _env_seconds("GROK_REGISTER_CONSOLE_STALL_TIMEOUT", 300)
```

- [ ] **Step 5: Add activity parsing helper in both modules**

```python
def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _task_is_stalled(last_log_at: str | None) -> bool:
    activity_at = _parse_timestamp(last_log_at)
    if activity_at is None:
        return False
    return (datetime.now() - activity_at).total_seconds() >= STALL_TIMEOUT_SECONDS
```

- [ ] **Step 6: Implement explicit watchdog finalization in both supervisors**

Inside `_refresh_running()`, create `closed: set[int] = set()` beside `finished`. After persisting parsed state and polling the process, handle the live/stalled branch before normal exit classification:

```python
exit_code = managed.process.poll()
if exit_code is None:
    if not _task_is_stalled(parsed["last_log_at"]):
        continue

    timeout_error = f"任务连续 {STALL_TIMEOUT_SECONDS} 秒无日志进展，已自动终止"
    self._terminate_process(managed)
    exit_code = self._close_managed(managed)
    closed.add(task_id)
    stalled_status = STATUS_PARTIAL if parsed["completed_count"] > 0 else STATUS_FAILED
    execute_no_return(
        """
        UPDATE tasks
        SET status = ?, finished_at = ?, exit_code = ?,
            completed_count = ?, failed_count = ?, current_round = ?,
            current_phase = ?, last_email = ?, last_error = ?,
            last_log_at = ?, pid = NULL
        WHERE id = ?
        """,
        (
            stalled_status,
            now_iso(),
            exit_code,
            parsed["completed_count"],
            parsed["failed_count"],
            parsed["current_round"],
            "stalled_timeout",
            parsed["last_email"],
            timeout_error,
            parsed["last_log_at"],
            task_id,
        ),
    )
    finished.append(task_id)
    continue
```

Modify the cleanup loop so already-closed stalled processes are only removed:

```python
for task_id in finished:
    with self._lock:
        managed = self._processes.pop(task_id, None)
    if managed and task_id not in closed:
        self._close_managed(managed)
```

- [ ] **Step 7: Run watchdog tests and verify GREEN**

Run the command from Step 3.

Expected: all watchdog tests pass.

- [ ] **Step 8: Commit watchdog behavior**

```bash
git add -- app/products/web/admin/register.py grok_helper/register.py tests/test_register_console.py tests/test_grok_helper_standalone.py
git commit -m "fix: terminate stalled register tasks"
```

### Task 3: Xvfb Cleanup And Operator Configuration

**Files:**
- Modify: `tests/test_grok_helper_standalone.py`
- Modify: `DrissionPage_example.py:79-88, 1250-1260`
- Modify: `.env.example`
- Modify: `docker-compose.yml`

- [ ] **Step 1: Add failing static cleanup/config tests**

Add these tests to `tests/test_grok_helper_standalone.py`:

```python
def test_runner_stops_virtual_display_during_shutdown(self):
    source = Path("DrissionPage_example.py").read_text(encoding="utf-8")
    self.assertIn("def stop_virtual_display():", source)
    self.assertIn("_virtual_display.stop()", source)
    finally_pos = source.rindex("    finally:")
    self.assertLess(
        source.index("stop_browser()", finally_pos),
        source.index("stop_virtual_display()", finally_pos),
    )

def test_compose_exposes_register_stall_timeout(self):
    compose = Path("docker-compose.yml").read_text(encoding="utf-8")
    self.assertIn(
        "GROK_REGISTER_CONSOLE_STALL_TIMEOUT: ${GROK_REGISTER_CONSOLE_STALL_TIMEOUT:-300}",
        compose,
    )

def test_env_example_documents_register_stall_timeout(self):
    env_example = Path(".env.example").read_text(encoding="utf-8")
    self.assertIn("GROK_REGISTER_CONSOLE_STALL_TIMEOUT=300", env_example)
```

- [ ] **Step 2: Run cleanup/config tests and verify RED**

Run:

```bash
python3 -m unittest \
  tests.test_grok_helper_standalone.StandaloneRegisterServiceTests.test_runner_stops_virtual_display_during_shutdown \
  tests.test_grok_helper_standalone.StandaloneRegisterServiceTests.test_compose_exposes_register_stall_timeout \
  tests.test_grok_helper_standalone.StandaloneRegisterServiceTests.test_env_example_documents_register_stall_timeout -v
```

Expected: all three tests fail because cleanup and configuration exposure are absent.

- [ ] **Step 3: Add normal Xvfb cleanup**

Add this helper after the display startup block in `DrissionPage_example.py`:

```python
def stop_virtual_display():
    global _virtual_display
    if _virtual_display is None:
        return
    try:
        _virtual_display.stop()
    except Exception:
        pass
    _virtual_display = None
```

Call it after `stop_browser()` in the outer `main()` cleanup:

```python
        stop_browser()
        stop_virtual_display()
```

- [ ] **Step 4: Expose the timeout setting**

Add this environment entry beside the existing supervisor settings in `docker-compose.yml`:

```yaml
      GROK_REGISTER_CONSOLE_STALL_TIMEOUT: ${GROK_REGISTER_CONSOLE_STALL_TIMEOUT:-300}
```

Add this documented default beside register runtime settings in `.env.example`:

```dotenv
# 注册子进程连续无日志进展的自动终止时间（秒）
GROK_REGISTER_CONSOLE_STALL_TIMEOUT=300
```

- [ ] **Step 5: Run cleanup/config tests and verify GREEN**

Run the command from Step 2.

Expected: all three tests pass.

- [ ] **Step 6: Commit cleanup and configuration**

```bash
git add -- DrissionPage_example.py .env.example docker-compose.yml tests/test_grok_helper_standalone.py
git commit -m "fix: clean up register task display runtime"
```

### Task 4: Full Verification And Completion Audit

**Files:**
- Verify: `app/products/web/admin/register.py`
- Verify: `grok_helper/register.py`
- Verify: `DrissionPage_example.py`
- Verify: `tests/test_register_console.py`
- Verify: `tests/test_grok_helper_standalone.py`
- Verify: `.env.example`
- Verify: `docker-compose.yml`

- [ ] **Step 1: Run both complete register suites**

```bash
python3 -m unittest tests.test_register_console tests.test_grok_helper_standalone -v
```

Expected: all tests pass with zero failures and zero errors.

- [ ] **Step 2: Run standalone syntax compilation**

```bash
python3 -m py_compile grok_helper/register.py app/products/web/admin/register.py DrissionPage_example.py
```

Expected: exit code 0 and no output.

- [ ] **Step 3: Verify mirrored watchdog symbols and configuration**

```bash
rg -n "STALL_TIMEOUT_SECONDS|stalled_timeout|连续 .* 秒无日志进展|_file_mtime_iso" \
  grok_helper/register.py app/products/web/admin/register.py
rg -n "GROK_REGISTER_CONSOLE_STALL_TIMEOUT" docker-compose.yml .env.example
rg -n "stop_virtual_display|_virtual_display.stop" DrissionPage_example.py
```

Expected: both register modules contain the watchdog paths, both configuration files expose the setting, and the runner contains display cleanup.

- [ ] **Step 4: Audit staged and committed paths**

```bash
git status --short
git log --oneline -5
```

Expected: the watchdog commits are present. Pre-existing unrelated worktree changes remain unstaged and uncommitted.

- [ ] **Step 5: Inspect the cumulative watchdog diff**

```bash
git diff 57e55de..HEAD -- \
  app/products/web/admin/register.py \
  grok_helper/register.py \
  DrissionPage_example.py \
  tests/test_register_console.py \
  tests/test_grok_helper_standalone.py \
  .env.example \
  docker-compose.yml \
  docs/superpowers/plans/2026-07-11-register-task-stall-watchdog.md
```

Expected: only the approved watchdog, truthful activity timestamp, Xvfb cleanup, configuration, tests, and implementation plan changes appear for these paths.

## Self-Review

- Spec coverage: Tasks 1-3 cover every requirement in the approved design, including real activity timestamps, configurable inactivity detection, partial/failed terminal semantics, process-group termination, PID/exit metadata, Xvfb cleanup, and regression tests.
- Placeholder scan: every test, implementation change, command, expected failure, expected success, and commit path is explicit.
- Type consistency: both modules use `STALL_TIMEOUT_SECONDS`, `_file_mtime_iso()`, `_parse_timestamp()`, `_task_is_stalled()`, and `current_phase = "stalled_timeout"` consistently.
- Scope: no runner protocol redesign, automatic retry, deployment, or unrelated refactor is included.
