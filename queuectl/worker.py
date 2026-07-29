from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
from typing import Optional

from .store import (
    claim_next_job,
    connect,
    extend_lease,
    finish_job,
    get_config,
    initialize,
    mark_worker_stopped,
    maintenance,
    register_worker,
    worker_stop_requested,
    touch_worker,
)


def _eprint(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def run_worker_process(count: int) -> None:
    if count < 1:
        raise ValueError("count must be at least 1")
    child_processes = []
    for index in range(count - 1):
        child_processes.append(_spawn_worker_child(index))
    try:
        _run_worker_slot(worker_count=count, child_index=None)
    finally:
        _stop_child_processes(child_processes)
        for child in child_processes:
            try:
                child.wait(timeout=5)
            except Exception:
                pass


def run_worker_child() -> None:
    _run_worker_slot(worker_count=1, child_index=0)


def _spawn_worker_child(index: int) -> subprocess.Popen:
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "queuectl",
            "worker",
            "__child",
        ],
        env={
            **os.environ,
            "QUEUECTL_WORKER_SLOT": str(index),
        },
    )


def _stop_child_processes(child_processes: list[subprocess.Popen]) -> None:
    for child in child_processes:
        try:
            os.kill(child.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            pass


def _run_worker_slot(worker_count: int, child_index: Optional[int]) -> None:
    conn = connect()
    initialize(conn)
    pid = os.getpid()
    stop_event = threading.Event()

    def handle_signal(signum, frame):
        stop_event.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)
    register_worker(conn, pid)
    conn.commit()
    config = get_config(conn)
    lease_seconds = int(config["lease-seconds"])
    heartbeat_seconds = int(config["heartbeat-seconds"])
    idle_poll_seconds = int(config["idle-poll-seconds"])
    if child_index is None:
        _eprint(f"queuectl worker {pid} started with {worker_count} slot(s)")
    else:
        _eprint(f"queuectl worker child {pid} started")

    reaper = threading.Thread(
        target=_reaper_loop,
        args=(pid, stop_event, heartbeat_seconds, idle_poll_seconds),
        daemon=True,
    )
    reaper.start()
    try:
        while not stop_event.is_set():
            maintenance(conn)
            conn.commit()
            job = claim_next_job(conn, pid, lease_seconds)
            conn.commit()
            if job is None:
                stop_event.wait(idle_poll_seconds)
                continue
            _run_job(conn, pid, job, stop_event, lease_seconds, heartbeat_seconds)
    finally:
        stop_event.set()
        reaper.join(timeout=max(1, heartbeat_seconds) + 1)
        mark_worker_stopped(conn, pid)
        conn.commit()
        conn.close()
        if child_index is None:
            _eprint(f"queuectl worker {pid} stopped")
        else:
            _eprint(f"queuectl worker child {pid} stopped")


def _run_job(
    conn,
    pid: int,
    job,
    stop_event: threading.Event,
    lease_seconds: int,
    heartbeat_seconds: int,
) -> None:
    job_id = job["id"]
    process = subprocess.Popen(
        job["command"],
        shell=True,
        start_new_session=True,
    )
    watchdog = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "queuectl._watchdog",
            str(pid),
            str(process.pid),
            str(max(1, heartbeat_seconds)),
        ]
    )
    done = threading.Event()

    def heartbeat_loop() -> None:
        hb_conn = connect()
        initialize(hb_conn)
        while not done.wait(max(1, heartbeat_seconds)):
            try:
                extend_lease(hb_conn, job_id, pid, lease_seconds)
                hb_conn.commit()
            except Exception:
                pass
        hb_conn.close()

    heartbeat = threading.Thread(target=heartbeat_loop, daemon=True)
    heartbeat.start()
    try:
        returncode = process.wait()
        done.set()
        heartbeat.join(timeout=max(1, heartbeat_seconds) + 1)
        try:
            watchdog.terminate()
            watchdog.wait(timeout=5)
        except Exception:
            pass
        result = finish_job(
            conn,
            job_id,
            pid,
            returncode=returncode,
            error_text=None if returncode == 0 else f"exit code {returncode}",
        )
        conn.commit()
        if returncode == 0:
            _eprint(f"job {job_id} completed")
        else:
            _eprint(f"job {job_id} failed -> {result['state']}")
    except Exception as exc:
        done.set()
        try:
            process.kill()
            process.wait(timeout=5)
        except Exception:
            pass
        try:
            watchdog.terminate()
        except Exception:
            pass
        _eprint(f"job {job_id} crashed in worker: {exc}")


def _reaper_loop(pid: int, stop_event: threading.Event, heartbeat_seconds: int, idle_poll_seconds: int) -> None:
    conn = connect()
    initialize(conn)
    while not stop_event.is_set():
        maintenance(conn)
        if worker_stop_requested(conn, pid):
            stop_event.set()
            break
        touch_worker(conn, pid)
        conn.commit()
        stop_event.wait(max(1, min(heartbeat_seconds, idle_poll_seconds)))
    conn.close()
