import importlib
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch


def import_fresh_register(env: dict[str, str] | None = None):
    for module_name in ("grok_helper.register", "grok_helper.paths", "grok_helper.logging"):
        sys.modules.pop(module_name, None)
    with patch.dict(os.environ, env or {}, clear=True):
        return importlib.import_module("grok_helper.register")


class StandaloneRegisterServiceTests(unittest.TestCase):
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

    def test_register_module_imports_without_grok2api_platform(self):
        register = import_fresh_register()

        self.assertNotIn("app.platform.logging.logger", Path(register.__file__).read_text(encoding="utf-8"))
        self.assertNotIn("app.platform.paths", Path(register.__file__).read_text(encoding="utf-8"))

    def test_default_database_path_uses_app_data_register_console_db(self):
        register = import_fresh_register()

        self.assertEqual(register.REGISTER_ROOT, Path("/app/data/register"))
        self.assertEqual(register.TASKS_DIR, Path("/app/data/register/tasks"))
        self.assertEqual(register.DB_PATH, Path("/app/data/register/console.db"))

    def test_main_mounts_register_api_at_admin_register_prefix(self):
        sys.modules.pop("main", None)
        main = importlib.import_module("main")

        route_paths = {route.path for route in main.app.routes}

        self.assertIn("/admin/register/tasks", route_paths)
        self.assertIn("/admin/register/settings", route_paths)
        self.assertNotIn("/admin/api/register/tasks", route_paths)

    def test_main_loads_dotenv_before_register_import(self):
        source = Path("main.py").read_text(encoding="utf-8")

        self.assertLess(source.index("load_dotenv()"), source.index("from grok_helper.register"))

    def test_source_defaults_merge_config_json_and_environment(self):
        register = import_fresh_register()

        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp)
            (source / "config.json").write_text(
                json.dumps(
                    {
                        "run": {"count": 12},
                        "proxy": "http://from-config:8118",
                        "browser_proxy": "",
                        "temp_mail_provider": "cloudmail",
                        "temp_mail_api_base": "https://mail.example.com",
                        "temp_mail_domain": "mail.example.com",
                        "api": {
                            "endpoint": "http://old.example/admin/api/tokens",
                            "token": "from-config",
                            "append": False,
                        },
                    }
                ),
                encoding="utf-8",
            )
            with (
                patch.object(register, "SOURCE_PROJECT", source),
                patch.dict(
                    os.environ,
                    {
                        "GROK_REGISTER_DEFAULT_RUN_COUNT": "7",
                        "GROK_REGISTER_DEFAULT_PROXY": "http://from-env:8118",
                        "GROK_REGISTER_DEFAULT_API_ENDPOINT": "http://sink.example/admin/api/tokens",
                        "GROK_REGISTER_DEFAULT_API_APPEND": "true",
                    },
                    clear=True,
                ),
            ):
                defaults = register.load_source_defaults()

        self.assertEqual(defaults["run"]["count"], 7)
        self.assertEqual(defaults["proxy"], "http://from-env:8118")
        self.assertEqual(defaults["temp_mail_provider"], "cloudmail")
        self.assertEqual(defaults["api"]["endpoint"], "http://sink.example/admin/api/tokens")
        self.assertTrue(defaults["api"]["append"])

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

    def test_stall_timeout_defaults_to_300_seconds(self):
        register = import_fresh_register()

        self.assertEqual(register.STALL_TIMEOUT_SECONDS, 300)

    def test_stall_timeout_can_be_overridden(self):
        register = import_fresh_register({"GROK_REGISTER_CONSOLE_STALL_TIMEOUT": "90"})

        self.assertEqual(register.STALL_TIMEOUT_SECONDS, 90)

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

    def test_refresh_running_marks_stalled_task_partial(self):
        register = import_fresh_register()

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "register"
            with (
                patch.object(register, "REGISTER_ROOT", root),
                patch.object(register, "TASKS_DIR", root / "tasks"),
                patch.object(register, "DB_PATH", root / "console.db"),
                patch.object(register, "STALL_TIMEOUT_SECONDS", 60, create=True),
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

    def test_refresh_running_does_not_reclassify_stopping_task_as_stalled(self):
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
                    register, root, completed_count=0, log_age_seconds=120
                )
                register.execute_no_return(
                    "UPDATE tasks SET status = ? WHERE id = ?",
                    (register.STATUS_STOPPING, task_id),
                )
                with patch.object(register.os, "killpg") as killpg:
                    supervisor._refresh_running()
                row = register.task_row(task_id)

                self.assertEqual(row["status"], register.STATUS_STOPPING)
                self.assertIn(task_id, supervisor._processes)
                self.assertFalse(log_handle.closed)
                killpg.assert_not_called()
                log_handle.close()
                supervisor._processes.clear()

    def test_requirements_include_api_and_browser_runtime_dependencies(self):
        requirements = Path("requirements.txt").read_text(encoding="utf-8")

        for package in ("fastapi", "granian", "python-dotenv", "requests[socks]", "DrissionPage"):
            self.assertIn(package, requirements)

    def test_dockerfile_runs_standalone_register_app(self):
        dockerfile = Path("Dockerfile").read_text(encoding="utf-8")

        self.assertIn("FROM python:3.12-slim", dockerfile)
        self.assertIn("google-chrome-stable", dockerfile)
        self.assertIn("COPY grok_helper ./grok_helper", dockerfile)
        self.assertIn("COPY main.py config.example.json DrissionPage_example.py email_register.py ./", dockerfile)
        self.assertIn('CMD ["granian"', dockerfile)
        self.assertIn("main:app", dockerfile)
        self.assertNotIn("app.main:app", dockerfile)

    def test_compose_contains_only_register_stack_services(self):
        compose = Path("docker-compose.yml").read_text(encoding="utf-8")

        self.assertIn("grok-helper:", compose)
        self.assertIn("GROK_REGISTER_SOURCE_DIR: /app", compose)
        self.assertIn("GROK_REGISTER_PYTHON: /usr/local/bin/python", compose)
        self.assertNotIn("grok2api:", compose)
        self.assertNotIn("grok2api-init-config:", compose)
        self.assertNotIn("  warp-proxy:", compose)
        self.assertNotIn("  privoxy:", compose)
        self.assertNotIn("  flaresolverr:", compose)


if __name__ == "__main__":
    unittest.main()
