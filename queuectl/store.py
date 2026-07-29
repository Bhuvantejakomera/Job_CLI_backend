from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

DEFAULTS = {
    "max-retries": "3",
    "backoff-base": "2",
    "lease-seconds": "20",
    "heartbeat-seconds": "5",
    "idle-poll-seconds": "1",
}

VALID_CONFIG_KEYS = set(DEFAULTS)
JOB_STATES = {"pending", "processing", "completed", "failed", "dead"}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat().replace("+00:00", "Z")


def iso_from(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_iso(value: str) -> datetime:
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value)


def seconds_from_base(base: int, completed_attempts: int) -> int:
    return int(base ** completed_attempts)


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def default_home() -> Path:
    return Path(os.environ.get("QUEUECTL_HOME", Path.cwd() / ".queuectl"))


def db_path() -> Path:
    env = os.environ.get("QUEUECTL_DB_PATH")
    if env:
        return Path(env)
    return default_home() / "queuectl.db"


def connect(path: Optional[Path] = None) -> sqlite3.Connection:
    db_file = path or db_path()
    db_file.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_file), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def initialize(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS config (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            command TEXT NOT NULL,
            state TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            max_retries INTEGER NOT NULL,
            backoff_base INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            available_at TEXT NOT NULL,
            lease_until TEXT,
            claimed_by INTEGER,
            last_error TEXT,
            completed_at TEXT
        );

        CREATE TABLE IF NOT EXISTS workers (
            pid INTEGER PRIMARY KEY,
            started_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            state TEXT NOT NULL,
            host TEXT NOT NULL,
            stop_requested_at TEXT
        );
        """
    )
    worker_columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(workers)").fetchall()
    }
    if "stop_requested_at" not in worker_columns:
        conn.execute("ALTER TABLE workers ADD COLUMN stop_requested_at TEXT")
    for key, value in DEFAULTS.items():
        conn.execute(
            "INSERT OR IGNORE INTO config(key, value) VALUES (?, ?)",
            (key, value),
        )


def get_config(conn: sqlite3.Connection) -> Dict[str, int]:
    rows = conn.execute("SELECT key, value FROM config").fetchall()
    values = {row["key"]: row["value"] for row in rows}
    merged = {key: int(values.get(key, default)) for key, default in DEFAULTS.items()}
    return merged


def set_config(conn: sqlite3.Connection, key: str, value: int) -> None:
    if key not in VALID_CONFIG_KEYS:
        raise ValueError(f"unsupported config key: {key}")
    conn.execute(
        """
        INSERT INTO config(key, value) VALUES(?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, str(value)),
    )


@contextmanager
def transaction(conn: sqlite3.Connection):
    # BEGIN IMMEDIATE is the key concurrency primitive in this project.
    # It acquires SQLite's RESERVED write lock up front, before any
    # SELECT/UPDATE logic runs. That means if two OS processes try to
    # claim work at the same time, only one can enter this critical
    # section. The second process blocks until the first one commits or
    # rolls back, which prevents duplicate claims across worker processes.
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def _job_row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "command": row["command"],
        "state": row["state"],
        "attempts": row["attempts"],
        "max_retries": row["max_retries"],
        "backoff_base": row["backoff_base"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "available_at": row["available_at"],
        "lease_until": row["lease_until"],
        "claimed_by": row["claimed_by"],
        "last_error": row["last_error"],
        "completed_at": row["completed_at"],
    }


def maintenance(conn: sqlite3.Connection) -> None:
    now = iso_now()
    with transaction(conn) as tx:
        tx.execute(
            """
            UPDATE jobs
               SET state = 'pending',
                   lease_until = NULL,
                   claimed_by = NULL,
                   updated_at = ?,
                   available_at = ?
             WHERE state = 'processing'
               AND lease_until IS NOT NULL
               AND lease_until <= ?
            """,
            (now, now, now),
        )
        tx.execute(
            """
            UPDATE jobs
               SET state = 'pending',
                   lease_until = NULL,
                   claimed_by = NULL,
                   updated_at = ?,
                   available_at = ?
             WHERE state = 'failed'
               AND available_at <= ?
            """,
            (now, now, now),
        )


def register_worker(conn: sqlite3.Connection, pid: int) -> None:
    now = iso_now()
    conn.execute(
        """
        INSERT INTO workers(pid, started_at, last_seen_at, state, host, stop_requested_at)
        VALUES(?, ?, ?, 'running', ?, NULL)
        ON CONFLICT(pid) DO UPDATE SET
            last_seen_at = excluded.last_seen_at,
            state = 'running',
            host = excluded.host,
            stop_requested_at = NULL
        """,
        (pid, now, now, socket.gethostname()),
    )


def touch_worker(conn: sqlite3.Connection, pid: int) -> None:
    now = iso_now()
    conn.execute(
        "UPDATE workers SET last_seen_at = ? WHERE pid = ?",
        (now, pid),
    )


def mark_worker_stopped(conn: sqlite3.Connection, pid: int) -> None:
    conn.execute(
        "UPDATE workers SET state = 'stopped', last_seen_at = ?, stop_requested_at = NULL WHERE pid = ?",
        (iso_now(), pid),
    )


def request_worker_stop(conn: sqlite3.Connection, pid: int) -> None:
    conn.execute(
        """
        UPDATE workers
           SET state = 'stopping',
               stop_requested_at = ?,
               last_seen_at = ?
         WHERE pid = ?
           AND state = 'running'
        """,
        (iso_now(), iso_now(), pid),
    )


def worker_stop_requested(conn: sqlite3.Connection, pid: int) -> bool:
    row = conn.execute(
        "SELECT stop_requested_at, state FROM workers WHERE pid = ?",
        (pid,),
    ).fetchone()
    if not row:
        return False
    return row["stop_requested_at"] is not None or row["state"] == "stopping"


def cleanup_dead_workers(conn: sqlite3.Connection) -> None:
    rows = conn.execute("SELECT pid FROM workers WHERE state = 'running'").fetchall()
    for row in rows:
        if not pid_alive(int(row["pid"])):
            conn.execute(
                "UPDATE workers SET state = 'stopped', last_seen_at = ? WHERE pid = ?",
                (iso_now(), int(row["pid"])),
            )


def live_workers(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    cleanup_dead_workers(conn)
    rows = conn.execute(
        "SELECT pid, started_at, last_seen_at, state, host, stop_requested_at FROM workers WHERE state = 'running' ORDER BY pid"
    ).fetchall()
    return [dict(row) for row in rows]


def count_jobs(conn: sqlite3.Connection) -> Dict[str, int]:
    rows = conn.execute("SELECT state, COUNT(*) AS count FROM jobs GROUP BY state").fetchall()
    counts = {state: 0 for state in JOB_STATES}
    for row in rows:
        counts[row["state"]] = row["count"]
    return counts


def enqueue_job(
    conn: sqlite3.Connection,
    job_id: str,
    command: str,
    max_retries: Optional[int] = None,
    backoff_base: Optional[int] = None,
) -> Dict[str, Any]:
    config = get_config(conn)
    now = iso_now()
    row = {
        "id": job_id,
        "command": command,
        "state": "pending",
        "attempts": 0,
        "max_retries": int(max_retries if max_retries is not None else config["max-retries"]),
        "backoff_base": int(backoff_base if backoff_base is not None else config["backoff-base"]),
        "created_at": now,
        "updated_at": now,
        "available_at": now,
    }
    conn.execute(
        """
        INSERT INTO jobs(
            id, command, state, attempts, max_retries, backoff_base,
            created_at, updated_at, available_at
        ) VALUES(
            :id, :command, :state, :attempts, :max_retries, :backoff_base,
            :created_at, :updated_at, :available_at
        )
        """,
        row,
    )
    return row


def list_jobs(conn: sqlite3.Connection, state: Optional[str] = None) -> List[Dict[str, Any]]:
    maintenance(conn)
    if state:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE state = ? ORDER BY created_at, id",
            (state,),
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM jobs ORDER BY created_at, id").fetchall()
    return [_job_row_to_dict(row) for row in rows]


def get_job(conn: sqlite3.Connection, job_id: str) -> Optional[Dict[str, Any]]:
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if not row:
        return None
    return _job_row_to_dict(row)


def claim_next_job(conn: sqlite3.Connection, pid: int, lease_seconds: int) -> Optional[Dict[str, Any]]:
    now = iso_now()
    lease_until = iso_from(utc_now() + timedelta(seconds=lease_seconds))
    with transaction(conn) as tx:
        # First, reclaim any work whose lease expired, and reopen retryable
        # failures whose backoff delay has elapsed. Doing this inside the same
        # transaction as the claim keeps the queue self-healing under load.
        tx.execute(
            """
            UPDATE jobs
               SET state = 'pending',
                   lease_until = NULL,
                   claimed_by = NULL,
                   updated_at = ?,
                   available_at = ?
             WHERE state = 'processing'
               AND lease_until IS NOT NULL
               AND lease_until <= ?
            """,
            (now, now, now),
        )
        tx.execute(
            """
            UPDATE jobs
               SET state = 'pending',
                   lease_until = NULL,
                   claimed_by = NULL,
                   updated_at = ?,
                   available_at = ?
             WHERE state = 'failed'
               AND available_at <= ?
            """,
            (now, now, now),
        )
        row = tx.execute(
            """
            SELECT *
              FROM jobs
             WHERE state = 'pending'
             ORDER BY created_at, id
             LIMIT 1
            """
        ).fetchone()
        if row is None:
            return None
        # The job is still pending here because the BEGIN IMMEDIATE lock
        # serialized all competing claimers. This update is the final step
        # that transitions the chosen row into processing.
        tx.execute(
            """
            UPDATE jobs
               SET state = 'processing',
                   claimed_by = ?,
                   lease_until = ?,
                   updated_at = ?
             WHERE id = ?
            """,
            (pid, lease_until, now, row["id"]),
        )
        job = _job_row_to_dict(row)
        job.update(
            {
                "state": "processing",
                "claimed_by": pid,
                "lease_until": lease_until,
                "updated_at": now,
            }
        )
        return job


def extend_lease(conn: sqlite3.Connection, job_id: str, pid: int, lease_seconds: int) -> None:
    lease_until = iso_from(utc_now() + timedelta(seconds=lease_seconds))
    conn.execute(
        """
        UPDATE jobs
           SET lease_until = ?,
               updated_at = ?
         WHERE id = ?
           AND state = 'processing'
           AND claimed_by = ?
        """,
        (lease_until, iso_now(), job_id, pid),
    )


def finish_job(
    conn: sqlite3.Connection,
    job_id: str,
    pid: int,
    returncode: int,
    error_text: Optional[str] = None,
) -> Dict[str, Any]:
    now = iso_now()
    job = get_job(conn, job_id)
    if job is None:
        raise ValueError(f"job not found: {job_id}")

    attempts = int(job["attempts"]) + 1
    if returncode == 0:
        conn.execute(
            """
            UPDATE jobs
               SET state = 'completed',
                   attempts = ?,
                   lease_until = NULL,
                   claimed_by = NULL,
                   last_error = NULL,
                   completed_at = ?,
                   updated_at = ?,
                   available_at = ?
             WHERE id = ? AND claimed_by = ?
            """,
            (attempts, now, now, now, job_id, pid),
        )
        return get_job(conn, job_id) or job

    if attempts >= int(job["max_retries"]):
        conn.execute(
            """
            UPDATE jobs
               SET state = 'dead',
                   attempts = ?,
                   lease_until = NULL,
                   claimed_by = NULL,
                   last_error = ?,
                   updated_at = ?,
                   available_at = ?
             WHERE id = ? AND claimed_by = ?
            """,
            (attempts, error_text, now, now, job_id, pid),
        )
    else:
        backoff = seconds_from_base(int(job["backoff_base"]), attempts)
        available_at = iso_from(utc_now() + timedelta(seconds=backoff))
        conn.execute(
            """
            UPDATE jobs
               SET state = 'failed',
                   attempts = ?,
                   lease_until = NULL,
                   claimed_by = NULL,
                   last_error = ?,
                   updated_at = ?,
                   available_at = ?
             WHERE id = ? AND claimed_by = ?
            """,
            (attempts, error_text, now, available_at, job_id, pid),
        )
    return get_job(conn, job_id) or job


def retry_dead_job(conn: sqlite3.Connection, job_id: str) -> Dict[str, Any]:
    now = iso_now()
    with transaction(conn) as tx:
        row = tx.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise ValueError(f"job not found: {job_id}")
        if row["state"] != "dead":
            raise ValueError(f"job {job_id} is not in dead state")
        tx.execute(
            """
            UPDATE jobs
               SET state = 'pending',
                   attempts = 0,
                   lease_until = NULL,
                   claimed_by = NULL,
                   last_error = NULL,
                   completed_at = NULL,
                   updated_at = ?,
                   available_at = ?
             WHERE id = ?
            """,
            (now, now, job_id),
        )
        row = tx.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return _job_row_to_dict(row)


def format_jobs_table(jobs: Iterable[Dict[str, Any]]) -> str:
    jobs = list(jobs)
    if not jobs:
        return "No jobs found."
    headers = ["id", "state", "attempts", "max_retries", "available_at", "command"]
    widths = {header: len(header) for header in headers}
    for job in jobs:
        for header in headers:
            widths[header] = max(widths[header], len(str(job.get(header, ""))))
    lines = []
    lines.append("  ".join(header.ljust(widths[header]) for header in headers))
    lines.append("  ".join("-" * widths[header] for header in headers))
    for job in jobs:
        lines.append(
            "  ".join(
                str(job.get(header, "")).ljust(widths[header])
                for header in headers
            )
        )
    return "\n".join(lines)


def format_status(conn: sqlite3.Connection) -> str:
    counts = count_jobs(conn)
    workers = live_workers(conn)
    parts = ["QueueCTL status"]
    for state in ["pending", "processing", "failed", "completed", "dead"]:
        parts.append(f"{state}: {counts.get(state, 0)}")
    if workers:
        parts.append(
            "workers: "
            + ", ".join(f"pid={worker['pid']}" for worker in workers)
        )
    else:
        parts.append("workers: none")
    return "\n".join(parts)


def serialize_jobs(jobs: Iterable[Dict[str, Any]]) -> str:
    return json.dumps(list(jobs), indent=2, sort_keys=True)
