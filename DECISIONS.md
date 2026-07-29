# DECISIONS

## 1) Which exact line(s) prevent two workers from claiming the same job, and why is that atomic across separate OS processes?

The critical line is in `queuectl/store.py:149` inside `transaction()`, and the claim logic that uses it is in `queuectl/store.py:347-386`:

```python
conn.execute("BEGIN IMMEDIATE")
```

That line is used by `claim_next_job()`, which selects the next pending job and updates it to `processing` within the same transaction. `BEGIN IMMEDIATE` takes SQLite's write lock before the select/update pair runs, so a second process cannot enter the same claim path until the first one commits or rolls back. That makes claim-and-mark atomic across separate OS processes, not just threads.

## 2) A worker is SIGKILLed halfway through a job. Walk through, step by step, what state the job is in and how it eventually runs again. What is the worst-case delay before recovery?

1. The worker claims the job and updates it to `processing`.
2. The worker starts the shell command and a heartbeat extends the job's lease every few seconds.
3. If the worker is `SIGKILL`ed, the heartbeat stops immediately.
4. A watchdog child notices the worker PID disappeared and kills the job's shell process group, so the command does not keep running forever.
5. The job stays in `processing` until its lease expires.
6. Any later worker process runs maintenance, sees the expired lease, and moves the job back to `pending`.
7. The job is claimed again and runs from the beginning.

The default lease is 20 seconds, so worst-case recovery is under 20 seconds after the crash, which is comfortably under the assignment's 60 second requirement.

## 3) Does `dlq retry` reset attempts? Why is that the right call?

Yes. `dlq retry` resets `attempts` to `0`.

That makes the retry behave like a fresh enqueue of the same logical job, with a full retry budget available again. Keeping the old attempt count would cause an immediate or near-immediate return to `dead`, which is usually not what an operator wants when manually retrying something from the DLQ.

## 4) What designs did you consider and reject for worker stop (cross-process signaling), and why?

I considered:

- A pure file flag that workers poll.
- A socket/control-server design.
- Only killing the current shell session.
- Storing worker PIDs in SQLite and signaling them directly.

I rejected the file flag because it does not directly identify live worker processes and adds extra polling logic without helping recovery. I rejected the control-server approach because it is more moving parts than this assignment needs. I rejected "kill the shell session" because `worker stop` must work from a different terminal and across independent worker processes. I rejected "signals only" because OS permission checks can get in the way of a reliable local test and because the queue should not depend on the operator having direct kill permission. I chose a SQLite stop-request flag in `queuectl/store.py:240-261`, with best-effort `SIGTERM` in `queuectl/cli.py:121-136` as a fast path, because it is simple, explicit, and easy to explain: each live worker process registers its own PID in SQLite, `worker stop` marks those rows as stopping, and the worker's reaper thread notices and exits cleanly.

## 5) If priorities were added tomorrow (high-priority jobs jump the queue), which parts of your design survive unchanged and which break?

Survive unchanged:

- SQLite persistence
- atomic claiming with `BEGIN IMMEDIATE`
- leases and crash recovery
- worker stop via PID registry
- retry/DLQ state machine

Would change:

- the job selection query in `claim_next_job()`
- the schema, because I would add a `priority` column and probably an index on `(priority, available_at, created_at)`
- any reporting that assumes FIFO ordering

The rest of the system is already built around "select next eligible job, then atomically claim it", so priority would mostly be a scheduling rule change, not a rewrite.
