from __future__ import annotations

import os
import signal
import sys
import time


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def main() -> None:
    if len(sys.argv) != 4:
        raise SystemExit("usage: python -m queuectl._watchdog <worker-pid> <job-pid> <poll-seconds>")
    worker_pid = int(sys.argv[1])
    job_pid = int(sys.argv[2])
    poll_seconds = float(sys.argv[3])
    while True:
        if not pid_alive(worker_pid):
            try:
                os.killpg(job_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except PermissionError:
                pass
            return
        if not pid_alive(job_pid):
            return
        time.sleep(poll_seconds)


if __name__ == "__main__":
    main()
