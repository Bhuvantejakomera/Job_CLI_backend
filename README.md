# QueueCTL

QueueCTL is a small CLI background job queue built for the backend internship assignment.

## What it does

- Enqueues jobs from the CLI
- Runs multiple worker OS processes or worker slots in parallel
- Retries failed jobs with exponential backoff
- Moves permanently failed jobs to a dead letter queue
- Persists everything in SQLite so jobs survive restarts
- Recovers jobs that were interrupted by a worker crash

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

Change config:

```bash
./bin/queuectl config set max-retries 5
./bin/queuectl config set backoff-base 2
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

Demo recording link: `ADD_LINK_HERE`
