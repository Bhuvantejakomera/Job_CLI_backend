from __future__ import annotations

import argparse
import json
import os
import signal
import sqlite3
import sys
from typing import Any

from .store import (
    connect,
    enqueue_job,
    format_jobs_table,
    format_status,
    initialize,
    list_jobs,
    live_workers,
    maintenance,
    retry_dead_job,
    request_worker_stop,
    serialize_jobs,
    set_config,
)
from .worker import run_worker_process
from .worker import run_worker_child


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="queuectl")
    sub = parser.add_subparsers(dest="command", required=True)

    enqueue = sub.add_parser("enqueue", help="enqueue a job as JSON")
    enqueue.add_argument("payload", help='JSON like \'{"id":"job1","command":"sleep 2"}\'')

    worker = sub.add_parser("worker", help="manage workers")
    worker_sub = worker.add_subparsers(dest="worker_command", required=True)
    worker_start = worker_sub.add_parser("start", help="start workers in the foreground")
    worker_start.add_argument("--count", type=int, default=1)
    worker_sub.add_parser("stop", help="stop all live workers")
    worker_sub.add_parser("__child", help=argparse.SUPPRESS)

    list_cmd = sub.add_parser("list", help="list jobs")
    list_cmd.add_argument("--state", default="pending")
    list_cmd.add_argument("--json", action="store_true")

    dlq = sub.add_parser("dlq", help="dead letter queue operations")
    dlq_sub = dlq.add_subparsers(dest="dlq_command", required=True)
    dlq_list = dlq_sub.add_parser("list", help="list dead jobs")
    dlq_list.add_argument("--json", action="store_true")
    dlq_retry = dlq_sub.add_parser("retry", help="retry a dead job")
    dlq_retry.add_argument("id")

    config = sub.add_parser("config", help="view and update configuration")
    config_sub = config.add_subparsers(dest="config_command", required=True)
    config_set = config_sub.add_parser("set", help="set a config value")
    config_set.add_argument("key")
    config_set.add_argument("value")

    sub.add_parser("status", help="show a queue summary")
    return parser


def main(argv: Any = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    conn = None

    try:
        if args.command == "worker" and args.worker_command == "start":
            run_worker_process(args.count)
            return
        if args.command == "worker" and args.worker_command == "__child":
            run_worker_child()
            return

        conn = connect()
        initialize(conn)

        if args.command in {"list", "dlq", "status"}:
            maintenance(conn)
            conn.commit()

        if args.command == "enqueue":
            _handle_enqueue(conn, args.payload)
        elif args.command == "worker" and args.worker_command == "stop":
            _handle_worker_stop(conn)
        elif args.command == "list":
            _handle_list(conn, args.state, args.json)
        elif args.command == "dlq" and args.dlq_command == "list":
            _handle_list(conn, "dead", args.json)
        elif args.command == "dlq" and args.dlq_command == "retry":
            _handle_dlq_retry(conn, args.id)
        elif args.command == "config" and args.config_command == "set":
            _handle_config_set(conn, args.key, args.value)
        elif args.command == "status":
            _handle_status(conn)
        else:
            parser.print_help()
            raise SystemExit(1)

        conn.commit()
    except SystemExit:
        raise
    except (sqlite3.IntegrityError, sqlite3.OperationalError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc
    finally:
        if conn is not None:
            conn.close()


def _handle_enqueue(conn, payload: str) -> None:
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid JSON payload: {exc}") from exc
    if not isinstance(data, dict):
        raise SystemExit("enqueue payload must be a JSON object")
    if "id" not in data or "command" not in data:
        raise SystemExit("enqueue payload requires id and command")
    row = enqueue_job(
        conn,
        data["id"],
        data["command"],
        max_retries=data.get("max_retries"),
        backoff_base=data.get("backoff_base"),
        priority=data.get("priority", 0),
        run_at=data.get("run_at"),
        timeout_seconds=data.get("timeout_seconds"),
    )
    print(json.dumps(row, indent=2, sort_keys=True))


def _handle_worker_stop(conn) -> None:
    workers = live_workers(conn)
    if not workers:
        print("No live workers found.")
        return
    stopped = 0
    for worker in workers:
        pid = int(worker["pid"])
        request_worker_stop(conn, pid)
        stopped += 1
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    conn.commit()
    print(f"Requested stop for {stopped} worker process(es).")


def _handle_list(conn, state: str, json_flag: bool) -> None:
    jobs = list_jobs(conn, state=state)
    if json_flag:
        sys.stdout.write(serialize_jobs(jobs))
        sys.stdout.write("\n")
    else:
        print(format_jobs_table(jobs))


def _handle_dlq_retry(conn, job_id: str) -> None:
    job = retry_dead_job(conn, job_id)
    print(json.dumps(job, indent=2, sort_keys=True))


def _handle_config_set(conn, key: str, value: str) -> None:
    try:
        int_value = int(value)
    except ValueError as exc:
        raise SystemExit("config values must be integers") from exc
    set_config(conn, key, int_value)
    print(f"{key} set to {int_value}")


def _handle_status(conn) -> None:
    print(format_status(conn))
