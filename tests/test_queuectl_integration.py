from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from datetime import datetime, timedelta, timezone
from urllib.request import urlopen


REPO_ROOT = Path(__file__).resolve().parents[1]


class QueueCTLIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(prefix="queuectl-tests-")
        self.db_path = Path(self._tmpdir.name) / "queuectl.db"
        self.workers: list[subprocess.Popen] = []
        self.web_servers: list[subprocess.Popen] = []

    def tearDown(self) -> None:
        self._stop_all_workers()
        self._stop_all_web_servers()
        self._tmpdir.cleanup()

    def env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["QUEUECTL_DB_PATH"] = str(self.db_path)
        env["PYTHONUNBUFFERED"] = "1"
        return env

    def run_cli(self, *args: str, timeout: int = 60) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [sys.executable, "-m", "queuectl", *args],
            cwd=REPO_ROOT,
            env=self.env(),
            text=True,
            capture_output=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            raise AssertionError(
                "queuectl command failed:\n"
                f"$ queuectl {' '.join(args)}\n"
                f"stdout:\n{result.stdout}\n"
                f"stderr:\n{result.stderr}"
            )
        return result

    def parse_status(self) -> tuple[dict[str, int], list[int]]:
        output = self.run_cli("status").stdout.strip().splitlines()
        counts: dict[str, int] = {}
        worker_pids: list[int] = []
        for line in output[1:]:
            if line.startswith("workers: "):
                tail = line.removeprefix("workers: ").strip()
                if tail == "none":
                    worker_pids = []
                else:
                    worker_pids = [int(part.split("=")[1]) for part in tail.split(", ")]
                continue
            name, value = line.split(": ", 1)
            counts[name] = int(value)
        return counts, worker_pids

    def wait_for(self, predicate, timeout: float = 30.0, interval: float = 0.2) -> None:
        deadline = time.time() + timeout
        last_error = None
        while time.time() < deadline:
            try:
                if predicate():
                    return
            except Exception as exc:  # pragma: no cover - diagnostic path
                last_error = exc
            time.sleep(interval)
        raise AssertionError(f"condition not met before timeout: {last_error!r}")

    def wait_for_counts(self, expected: dict[str, int], timeout: float = 30.0) -> None:
        def predicate() -> bool:
            counts, _ = self.parse_status()
            return all(counts.get(key, 0) == value for key, value in expected.items())

        self.wait_for(predicate, timeout=timeout)

    def wait_for_worker_count(self, expected: int, timeout: float = 15.0) -> None:
        def predicate() -> bool:
            _, worker_pids = self.parse_status()
            return len(worker_pids) == expected

        self.wait_for(predicate, timeout=timeout)

    def enqueue(self, job_id: str, command: str) -> dict[str, object]:
        result = self.run_cli("enqueue", json.dumps({"id": job_id, "command": command}))
        return json.loads(result.stdout)

    def enqueue_payload(self, payload: dict[str, object]) -> dict[str, object]:
        result = self.run_cli("enqueue", json.dumps(payload))
        return json.loads(result.stdout)

    def start_worker(self, count: int = 1) -> subprocess.Popen:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "queuectl",
                "worker",
                "start",
                "--count",
                str(count),
            ],
            cwd=REPO_ROOT,
            env=self.env(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.workers.append(proc)
        time.sleep(0.5)
        if proc.poll() is not None:
            raise AssertionError(f"worker exited immediately with code {proc.returncode}")
        return proc

    def start_dashboard(self, port: int | None = None) -> tuple[subprocess.Popen, int]:
        if port is None:
            port = 8765
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "queuectl",
                "web",
                "serve",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            cwd=REPO_ROOT,
            env=self.env(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.web_servers.append(proc)
        deadline = time.time() + 15
        last_error = None
        while time.time() < deadline:
            if proc.poll() is not None:
                stderr = ""
                if proc.stderr is not None:
                    stderr = proc.stderr.read() or ""
                if "Operation not permitted" in stderr:
                    self.skipTest("web dashboard sockets are blocked in this environment")
                raise AssertionError(
                    f"dashboard exited immediately with code {proc.returncode}:\n{stderr}"
                )
            try:
                with urlopen(f"http://127.0.0.1:{port}/", timeout=2) as response:
                    if response.status == 200:
                        return proc, port
            except Exception as exc:
                last_error = exc
            time.sleep(0.2)
        raise AssertionError(f"dashboard did not become ready before timeout: {last_error!r}")

    def stop_worker_process(self, proc: subprocess.Popen) -> None:
        if proc.poll() is None:
            try:
                self.run_cli("worker", "stop", timeout=20)
            except AssertionError:
                pass
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)

    def _stop_all_workers(self) -> None:
        if not self.workers:
            return
        try:
            self.run_cli("worker", "stop", timeout=20)
        except AssertionError:
            pass
        for proc in self.workers:
            if proc.poll() is None:
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=10)
        self.workers.clear()

    def _stop_all_web_servers(self) -> None:
        if not self.web_servers:
            return
        for proc in self.web_servers:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=10)
        self.web_servers.clear()

    def test_basic_job_completes(self) -> None:
        self.start_worker()
        marker = Path(self._tmpdir.name) / "basic.txt"
        self.enqueue(
            "basic",
            f"python3 -c \"from pathlib import Path; Path(r'{marker}').write_text('ok')\"",
        )
        self.wait_for_counts({"completed": 1, "pending": 0, "processing": 0}, timeout=20)
        self.assertTrue(marker.exists())
        self.assertEqual(marker.read_text(), "ok")

    def test_scheduled_job_waits_until_run_at(self) -> None:
        self.start_worker()
        marker = Path(self._tmpdir.name) / "scheduled.txt"
        run_at = datetime.now(timezone.utc) + timedelta(seconds=3)
        payload = {
            "id": "scheduled",
            "command": (
                "python3 -c \"from pathlib import Path; "
                f"Path(r'{marker}').write_text('scheduled')\""
            ),
            "run_at": run_at.isoformat().replace("+00:00", "Z"),
        }
        self.enqueue_payload(payload)
        self.wait_for_counts({"pending": 1}, timeout=5)
        self.wait_for_counts({"completed": 1, "pending": 0, "processing": 0}, timeout=20)
        self.assertTrue(marker.exists())
        self.assertEqual(marker.read_text(), "scheduled")

    def test_priority_jobs_run_highest_first(self) -> None:
        self.start_worker()
        log_path = Path(self._tmpdir.name) / "priority.log"

        low_command = (
            "python3 -c \"from pathlib import Path; "
            f"p = Path(r'{log_path}'); "
            "p.write_text((p.read_text() if p.exists() else '') + 'low\\n')\""
        )
        high_command = (
            "python3 -c \"from pathlib import Path; "
            f"p = Path(r'{log_path}'); "
            "p.write_text((p.read_text() if p.exists() else '') + 'high\\n')\""
        )

        self.enqueue_payload({"id": "low", "command": low_command, "priority": 0})
        self.enqueue_payload({"id": "high", "command": high_command, "priority": 10})
        self.wait_for_counts({"completed": 2, "pending": 0, "processing": 0}, timeout=20)
        self.assertEqual(log_path.read_text().splitlines(), ["high", "low"])

    def test_timeout_moves_job_to_dead_letter_queue(self) -> None:
        self.start_worker()
        self.enqueue_payload(
            {
                "id": "timeout",
                "command": "sleep 5",
                "timeout_seconds": 1,
                "max_retries": 1,
            }
        )
        self.wait_for_counts({"dead": 1, "pending": 0, "processing": 0}, timeout=20)
        job = json.loads(self.run_cli("dlq", "list", "--json").stdout)[0]
        self.assertEqual(job["id"], "timeout")
        self.assertIn("timed out", job["last_error"])

    def test_job_output_is_captured_and_metrics_update(self) -> None:
        self.start_worker()
        self.enqueue_payload(
            {
                "id": "output",
                "command": (
                    "python3 -c \"import sys; "
                    "print('hello from stdout'); "
                    "print('hello from stderr', file=sys.stderr)\""
                ),
            }
        )
        self.wait_for_counts({"completed": 1, "pending": 0, "processing": 0}, timeout=20)

        job = json.loads(self.run_cli("job", "show", "output").stdout)
        self.assertEqual(job["stdout_text"].strip(), "hello from stdout")
        self.assertEqual(job["stderr_text"].strip(), "hello from stderr")
        self.assertEqual(job["exit_code"], 0)

        logs = self.run_cli("job", "logs", "output").stdout
        self.assertIn("stdout:", logs)
        self.assertIn("hello from stdout", logs)
        self.assertIn("stderr:", logs)
        self.assertIn("hello from stderr", logs)

        metrics = self.run_cli("metrics").stdout
        self.assertIn("jobs_completed: 1", metrics)
        self.assertIn("workers_running: 1", metrics)

    def test_web_dashboard_serves_status_and_metrics(self) -> None:
        self.start_worker()
        self.enqueue_payload(
            {
                "id": "dashboard",
                "command": (
                    "python3 -c \"from pathlib import Path; "
                    "print('dashboard ready')\""
                ),
            }
        )
        self.wait_for_counts({"completed": 1, "pending": 0, "processing": 0}, timeout=20)

        _, port = self.start_dashboard()
        with urlopen(f"http://127.0.0.1:{port}/", timeout=5) as response:
            html = response.read().decode("utf-8")
        self.assertIn("QueueCTL Dashboard", html)
        self.assertIn("dashboard", html)
        self.assertIn("Pending", html)

        with urlopen(f"http://127.0.0.1:{port}/metrics", timeout=5) as response:
            metrics = response.read().decode("utf-8")
        self.assertIn("jobs_completed: 1", metrics)

        with urlopen(f"http://127.0.0.1:{port}/jobs", timeout=5) as response:
            jobs_json = response.read().decode("utf-8")
        self.assertIn('"id": "dashboard"', jobs_json)

    def test_failed_job_retries_and_enters_dlq(self) -> None:
        self.start_worker()
        self.run_cli("config", "set", "max-retries", "2")
        self.run_cli("config", "set", "backoff-base", "2")
        self.enqueue("fail", "false")
        self.wait_for_counts({"dead": 1, "pending": 0, "processing": 0, "failed": 0}, timeout=20)

        dlq = json.loads(self.run_cli("dlq", "list", "--json").stdout)
        self.assertEqual(len(dlq), 1)
        self.assertEqual(dlq[0]["id"], "fail")
        self.assertEqual(dlq[0]["state"], "dead")
        self.assertEqual(dlq[0]["attempts"], 2)

    def test_many_jobs_across_multiple_workers_run_once(self) -> None:
        for _ in range(3):
            self.start_worker()
        marker_dir = Path(self._tmpdir.name) / "markers"
        marker_dir.mkdir()

        for index in range(20):
            marker = marker_dir / f"job-{index}.txt"
            command = (
                "python3 -c \"from pathlib import Path; "
                f"Path(r'{marker}').open('x').close()\""
            )
            self.enqueue(f"job-{index}", command)

        self.wait_for_counts({"completed": 20, "pending": 0, "processing": 0, "failed": 0, "dead": 0}, timeout=30)
        for index in range(20):
            self.assertTrue((marker_dir / f"job-{index}.txt").exists())

    def test_sigkill_mid_job_recovers_after_restart(self) -> None:
        proc = self.start_worker()
        self.enqueue("crash", "sleep 5")
        self.wait_for_counts({"processing": 1}, timeout=10)

        proc.kill()
        proc.wait(timeout=10)
        self.workers = [worker for worker in self.workers if worker is not proc]

        restarted = self.start_worker()
        self.wait_for_counts({"completed": 1, "processing": 0, "pending": 0}, timeout=45)
        self.assertIsNone(restarted.poll())

    def test_jobs_survive_restart(self) -> None:
        self.enqueue("persist-1", "python3 -c \"print('one')\"")
        self.enqueue("persist-2", "python3 -c \"print('two')\"")

        counts, _ = self.parse_status()
        self.assertEqual(counts.get("pending"), 2)

        self.run_cli("status")
        self.start_worker()
        self.wait_for_counts({"completed": 2, "pending": 0, "processing": 0}, timeout=20)


if __name__ == "__main__":
    unittest.main()
