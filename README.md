# QueueCTL

> A lightweight SQLite-backed CLI job queue built for a backend internship assignment.

📽️ **Demo:** [Watch the project in action](https://drive.google.com/file/d/1MIpd5geRZl0rO74iSuSB6spoqHNTOb0I/view?usp=sharing)

## What it does

- Enqueues jobs from the CLI
- Runs multiple worker OS processes or worker slots in parallel
- Retries failed jobs with exponential backoff
- Moves permanently failed jobs to a dead letter queue
- Persists everything in SQLite so jobs survive restarts
- Recovers jobs that were interrupted by a worker crash
- Supports scheduled jobs with `run_at`
- Supports priority queues
- Enforces per-job timeouts
- Captures stdout/stderr for each job run
- Exposes metrics and a minimal web dashboard

## Setup

```bash
python3 -m pip install -e .
```

If you want to run it directly from the repo without installing, use:

```bash
python3 -m queuectl --help
```

If editable install fails on your Mac's system Python, use the repo-local shim:

```bash
./bin/queuectl --help
```

## Usage

Enqueue a job:

```bash
./bin/queuectl enqueue '{"id":"job1","command":"echo hello"}'
```

Enqueue a scheduled high-priority job with a timeout:

```bash
./bin/queuectl enqueue '{"id":"job2","command":"sleep 5","priority":10,"run_at":"2026-07-29T10:00:00Z","timeout_seconds":30}'
```

Start two workers in the foreground:

```bash
./bin/queuectl worker start --count 2
```

Stop live workers from another terminal:

```bash
./bin/queuectl worker stop
```

List pending jobs as JSON:

```bash
./bin/queuectl list --state pending --json
```

Inspect the queue:

```bash
./bin/queuectl status
```

Retry a dead job:

```bash
./bin/queuectl dlq retry job1
```

Inspect a completed job:

```bash
./bin/queuectl job show job1
./bin/queuectl job logs job1
```

View metrics:

```bash
./bin/queuectl metrics
```

Launch the dashboard:

```bash
./bin/queuectl web serve --host 127.0.0.1 --port 8765
```

Change config:

```bash
./bin/queuectl config set max-retries 5
./bin/queuectl config set backoff-base 2
```

# CLI Command Reference

The CLI is designed to be used from multiple terminals at the same time. Commands below use the repo-local shim `./bin/queuectl`, which maps directly to the installed `queuectl` command.

| Terminal | Command | Purpose | Description |
|----------|---------|---------|-------------|
| Terminal 1 | `./bin/queuectl enqueue '{"id":"job1","command":"echo Hello"}'` | Enqueue Job | Adds a new background job to the queue with the initial `pending` state. The job is stored in SQLite and waits until a worker picks it up. |
| Terminal 1 | `./bin/queuectl enqueue '{"id":"job2","command":"sleep 5"}'` | Enqueue Another Job | Multiple jobs can be queued before or while workers are running, so you can build a backlog and observe parallel execution. |
| Terminal 2 | `./bin/queuectl worker start --count 3` | Start Workers | Starts three worker processes in the foreground. Workers continuously poll the queue, atomically claim pending jobs, execute them, and update their state in SQLite. |
| Terminal 3 | `./bin/queuectl status` | Queue Status | Displays a summary of the queue including Pending, Processing, Completed, Failed, Dead jobs, and the number of active workers. |
| Terminal 3 | `./bin/queuectl list --state pending` | List Pending Jobs | Shows all jobs currently waiting to be executed. |
| Terminal 3 | `./bin/queuectl list --state processing` | List Running Jobs | Displays jobs currently being processed by workers. |
| Terminal 3 | `./bin/queuectl list --state completed` | List Completed Jobs | Displays successfully completed jobs. |
| Terminal 3 | `./bin/queuectl list --state failed` | List Failed Jobs | Displays jobs waiting for their next retry according to the configured exponential backoff delay. |
| Terminal 3 | `./bin/queuectl list --state dead` | List Dead Jobs | Displays permanently failed jobs that have been moved to the Dead Letter Queue. |
| Terminal 3 | `./bin/queuectl list --state pending --json` | JSON Output | Outputs only a valid JSON array containing pending jobs. No additional logs should be printed to stdout. |
| Terminal 4 | `./bin/queuectl worker stop` | Stop Workers | Gracefully stops all running workers from another terminal. Workers finish their current job before exiting. |
| Terminal 5 | `./bin/queuectl dlq list` | View DLQ | Lists every job currently in the Dead Letter Queue. |
| Terminal 5 | `./bin/queuectl dlq retry job1` | Retry Dead Job | Moves a dead job back into the queue so it can be processed again according to the project's retry policy. |
| Terminal 5 | `./bin/queuectl job show job1` | Show Job | Displays the full job record, including scheduling, timeout, and captured output fields. |
| Terminal 5 | `./bin/queuectl job logs job1` | Show Logs | Prints the stored stdout and stderr for the selected job. |
| Terminal 6 | `./bin/queuectl metrics` | Queue Metrics | Emits a compact metrics summary with job counts and live worker counts. |
| Terminal 7 | `./bin/queuectl web serve --host 127.0.0.1 --port 8765` | Web Dashboard | Starts a minimal local dashboard that shows live queue status, recent jobs, and a metrics endpoint. |
| Terminal 6 | `./bin/queuectl config set max-retries 5` | Configure Retries | Updates the default maximum retry count and persists the configuration in SQLite. |
| Terminal 6 | `./bin/queuectl config set backoff-base 2` | Configure Backoff | Updates the exponential backoff base used when scheduling retries. |

# Typical Workflow

| Step | Terminal | Command | Expected Result |
|------|----------|---------|-----------------|
| 1 | Terminal 1 | Enqueue jobs | Jobs are stored as `pending`. |
| 2 | Terminal 2 | `./bin/queuectl worker start --count 3` | Workers begin polling and processing jobs. |
| 3 | Terminal 3 | `./bin/queuectl status` | Displays live queue statistics. |
| 4 | Terminal 3 | `./bin/queuectl list --state processing` | Shows currently executing jobs. |
| 5 | Terminal 3 | `./bin/queuectl list --state completed` | Shows completed jobs. |
| 6 | Terminal 5 | `./bin/queuectl dlq list` | View permanently failed jobs. |
| 7 | Terminal 5 | `./bin/queuectl dlq retry <job_id>` | Re-enqueue a dead job for processing. |
| 8 | Terminal 4 | `./bin/queuectl worker stop` | Workers complete their current job and shut down gracefully. |

# Multi-Terminal Demonstration

```text
                Terminal 1
        queuectl enqueue job1
        queuectl enqueue job2
        queuectl enqueue job3
                  │
                  ▼
          ┌──────────────────┐
          │   SQLite Queue   │
          │ pending jobs     │
          └──────────────────┘
                  ▲
                  │
        ┌─────────┴─────────┐
        │                   │
        ▼                   ▼
 Worker Process 1     Worker Process 2
        │                   │
        └─────────┬─────────┘
                  ▼
          Execute Commands
                  ▼
     completed / failed / dead
                  ▲
                  │
      Terminal 3 → status / list
      Terminal 5 → dlq commands
      Terminal 4 → worker stop
```

## Architecture

- SQLite stores jobs, config, and worker registrations.
- Job claiming is atomic because each claim runs inside `BEGIN IMMEDIATE`, which gives the process a write lock before it selects and updates the next job.
- Jobs that are actively processing get a lease. A heartbeat extends the lease while the worker is alive.
- If a worker dies, the job is returned to `pending` after the lease expires.
- A watchdog process kills the shell command if the worker process disappears, so a `SIGKILL`ed worker cannot leave a command running forever.
- `worker stop` writes a stop request into SQLite for every live worker and also tries `SIGTERM` as a best-effort fast path.
- Config values are snapshotted onto each job at enqueue time, so changing config later only affects new jobs.

## Testing

Run the integration suite with:

```bash
python3 -m unittest -v tests.test_queuectl_integration
```

The suite covers the main interview scenarios:

- basic job completion
- retry and DLQ behavior
- multiple workers processing jobs exactly once
- SIGKILL crash recovery
- persistence across process restarts

I also validated the core flows manually:

- happy-path completion
- retry and DLQ behavior
- cross-process worker stop
- crash recovery after killing a worker

## Submission Checklist

- Public GitHub repository
- Incremental git history
- Demo recording link

Demo recording link:
