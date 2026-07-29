from __future__ import annotations

import logging
import html
import json
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from .store import collect_metrics, format_metrics, initialize, list_jobs, maintenance, connect


logger = logging.getLogger(__name__)


def serve_dashboard(host: str = "127.0.0.1", port: int = 8765) -> None:
    server = ThreadingHTTPServer((host, port), _build_handler())
    logger.info("queuectl web dashboard listening on http://%s:%s", host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def _build_handler() -> type[BaseHTTPRequestHandler]:
    class DashboardHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            try:
                parsed = urlparse(self.path)
                if parsed.path == "/":
                    self._send_html(_render_dashboard())
                elif parsed.path == "/jobs":
                    self._send_json(_render_jobs_json())
                elif parsed.path == "/metrics":
                    self._send_text(_render_metrics_text())
                else:
                    self.send_error(404, "Not found")
            except sqlite3.DatabaseError as exc:
                logger.exception("dashboard database error: %s", exc)
                self._send_error_page(
                    "Database error",
                    "The queue database could not be read. The file may be corrupted or "
                    "in an inconsistent state. For your demo database, try deleting "
                    "the DB file and starting fresh.",
                )
            except sqlite3.OperationalError as exc:
                logger.exception("dashboard operational error: %s", exc)
                self._send_error_page(
                    "Database error",
                    f"SQLite reported an operational error: {exc}.",
                )

        def log_message(self, format: str, *args) -> None:  # noqa: A003
            logger.info("%s - %s", self.address_string(), format % args)

        def _send_html(self, body: str) -> None:
            encoded = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def _send_text(self, body: str) -> None:
            encoded = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def _send_json(self, body: str) -> None:
            encoded = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def _send_error_page(self, title: str, message: str) -> None:
            body = f"""
            <!doctype html>
            <html lang="en">
              <head>
                <meta charset="utf-8" />
                <meta name="viewport" content="width=device-width, initial-scale=1" />
                <title>{html.escape(title)}</title>
                <style>
                  body {{
                    font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
                    background: #0b1020;
                    color: #e5eefc;
                    margin: 0;
                    padding: 40px;
                  }}
                  .card {{
                    max-width: 760px;
                    margin: 0 auto;
                    padding: 24px;
                    border-radius: 16px;
                    background: rgba(17, 25, 44, 0.92);
                    border: 1px solid rgba(148, 163, 184, 0.2);
                  }}
                  h1 {{ margin-top: 0; }}
                  p {{ color: #b5c2de; line-height: 1.6; }}
                  code {{
                    display: block;
                    padding: 12px;
                    border-radius: 10px;
                    background: rgba(148, 163, 184, 0.12);
                    overflow-x: auto;
                  }}
                </style>
              </head>
              <body>
                <div class="card">
                  <h1>{html.escape(title)}</h1>
                  <p>{html.escape(message)}</p>
                  <p>For a fresh demo database on macOS:</p>
                  <code>rm -f /private/tmp/queuectl-demo.db</code>
                  <p>Then restart the dashboard with the same `QUEUECTL_DB_PATH`.</p>
                </div>
              </body>
            </html>
            """
            encoded = body.encode("utf-8")
            self.send_response(500)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    return DashboardHandler


def _render_dashboard() -> str:
    metrics = _metrics_snapshot()
    jobs = _jobs_snapshot(limit=12)
    cards = "\n".join(
        f"""
        <article class="card" data-metric="{html.escape(key)}">
          <div class="card-label">{html.escape(label)}</div>
          <div class="card-value">{value}</div>
        </article>
        """
        for key, label, value in (
            ("jobs_pending", "Pending", metrics["jobs_pending"]),
            ("jobs_pending_ready", "Ready", metrics["jobs_pending_ready"]),
            ("jobs_pending_scheduled", "Scheduled", metrics["jobs_pending_scheduled"]),
            ("jobs_processing", "Processing", metrics["jobs_processing"]),
            ("jobs_completed", "Completed", metrics["jobs_completed"]),
            ("jobs_failed", "Failed", metrics["jobs_failed"]),
            ("jobs_dead", "Dead", metrics["jobs_dead"]),
            ("workers_running", "Workers", metrics["workers_running"]),
        )
    )
    job_rows = []
    for job in jobs:
        job_rows.append(
            """
            <tr>
              <td>{id}</td>
              <td>{state}</td>
              <td>{priority}</td>
              <td>{run_at}</td>
              <td>{timeout}</td>
              <td>{attempts}</td>
              <td>{exit_code}</td>
            </tr>
            """.format(
                id=html.escape(str(job["id"])),
                state=html.escape(str(job["state"])),
                priority=html.escape(str(job["priority"])),
                run_at=html.escape(str(job["run_at"])),
                timeout=html.escape(str(job["timeout_seconds"] or "")),
                attempts=html.escape(str(job["attempts"])),
                exit_code=html.escape(str(job["exit_code"] or "")),
            )
        )
    dashboard_script = """
          <script>
            const metricKeys = [
              "jobs_pending",
              "jobs_pending_ready",
              "jobs_pending_scheduled",
              "jobs_processing",
              "jobs_completed",
              "jobs_failed",
              "jobs_dead",
              "workers_running",
            ];

            function escapeHtml(value) {
              return String(value)
                .replaceAll("&", "&amp;")
                .replaceAll("<", "&lt;")
                .replaceAll(">", "&gt;")
                .replaceAll('"', "&quot;")
                .replaceAll("'", "&#39;");
            }

            function parseMetrics(text) {
              const metrics = {};
              for (const line of text.split("\\n")) {
                const index = line.indexOf(": ");
                if (index === -1) continue;
                const key = line.slice(0, index).trim();
                const value = Number(line.slice(index + 2).trim());
                if (!Number.isNaN(value)) metrics[key] = value;
              }
              return metrics;
            }

            function renderJobs(jobs) {
              const body = document.getElementById("recent-jobs-body");
              if (!body) return;
              if (!jobs.length) {
                body.innerHTML = '<tr><td colspan="7" class="empty">No jobs found.</td></tr>';
                return;
              }
              body.innerHTML = jobs.map((job) => `
                <tr>
                  <td>${escapeHtml(job.id)}</td>
                  <td>${escapeHtml(job.state)}</td>
                  <td>${escapeHtml(job.priority)}</td>
                  <td>${escapeHtml(job.run_at)}</td>
                  <td>${escapeHtml(job.timeout_seconds ?? "")}</td>
                  <td>${escapeHtml(job.attempts)}</td>
                  <td>${escapeHtml(job.exit_code ?? "")}</td>
                </tr>
              `).join("");
            }

            async function refreshDashboard() {
              try {
                const [metricsResp, jobsResp] = await Promise.all([
                  fetch("/metrics", { cache: "no-store" }),
                  fetch("/jobs", { cache: "no-store" }),
                ]);
                const metrics = parseMetrics(await metricsResp.text());
                for (const key of metricKeys) {
                  const card = document.querySelector(`[data-metric="${key}"] .card-value`);
                  if (card && metrics[key] !== undefined) {
                    card.textContent = metrics[key];
                  }
                }
                renderJobs(await jobsResp.json());
              } catch (error) {
                console.error("dashboard refresh failed", error);
              }
            }

            refreshDashboard();
            setInterval(refreshDashboard, 2000);
          </script>
    """
    return f"""
    <!doctype html>
    <html lang="en">
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>QueueCTL Dashboard</title>
        <style>
          :root {{
            --bg: #0b1020;
            --panel: rgba(17, 25, 44, 0.88);
            --panel-border: rgba(148, 163, 184, 0.18);
            --text: #e5eefc;
            --muted: #91a4c7;
            --accent: #74c0fc;
            --accent-2: #8ce99a;
            --accent-3: #ffd43b;
          }}
          * {{ box-sizing: border-box; }}
          body {{
            margin: 0;
            font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
            background:
              radial-gradient(circle at top left, rgba(116, 192, 252, 0.18), transparent 30%),
              radial-gradient(circle at bottom right, rgba(140, 233, 154, 0.12), transparent 28%),
              linear-gradient(180deg, #09101f 0%, #050816 100%);
            color: var(--text);
          }}
          .shell {{
            max-width: 1180px;
            margin: 0 auto;
            padding: 32px 20px 48px;
          }}
          header {{
            display: flex;
            justify-content: space-between;
            align-items: end;
            gap: 16px;
            margin-bottom: 28px;
          }}
          h1 {{
            margin: 0;
            font-size: clamp(2rem, 4vw, 3.2rem);
            line-height: 1;
          }}
          .lede {{
            color: var(--muted);
            margin-top: 10px;
            max-width: 52rem;
          }}
          .badge {{
            padding: 8px 12px;
            border: 1px solid var(--panel-border);
            border-radius: 999px;
            color: var(--accent-2);
            background: rgba(140, 233, 154, 0.08);
            white-space: nowrap;
          }}
          .cards {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
            gap: 14px;
            margin: 24px 0;
          }}
          .card, .panel {{
            border: 1px solid var(--panel-border);
            background: var(--panel);
            backdrop-filter: blur(18px);
            border-radius: 20px;
            box-shadow: 0 20px 40px rgba(0, 0, 0, 0.18);
          }}
          .card {{
            padding: 18px;
          }}
          .card-label {{
            color: var(--muted);
            font-size: 0.85rem;
            margin-bottom: 10px;
            text-transform: uppercase;
            letter-spacing: 0.08em;
          }}
          .card-value {{
            font-size: 2rem;
            font-weight: 700;
          }}
          .panel {{
            padding: 18px;
            overflow: hidden;
          }}
          .panel h2 {{
            margin: 0 0 14px;
            font-size: 1.2rem;
          }}
          table {{
            width: 100%;
            border-collapse: collapse;
          }}
          th, td {{
            text-align: left;
            padding: 10px 8px;
            border-bottom: 1px solid rgba(148, 163, 184, 0.15);
            font-size: 0.94rem;
          }}
          th {{
            color: var(--muted);
            font-weight: 600;
          }}
          .links {{
            display: flex;
            gap: 12px;
            flex-wrap: wrap;
            margin-top: 16px;
          }}
          .links a {{
            color: var(--accent);
            text-decoration: none;
          }}
          .links a:hover {{
            text-decoration: underline;
          }}
          .empty {{
            color: var(--muted);
            padding: 16px 0 4px;
          }}
        </style>
      </head>
      <body>
        <main class="shell">
          <header>
            <div>
              <h1>QueueCTL Dashboard</h1>
              <p class="lede">A live view of pending work, worker activity, and recent job state. Scheduled jobs, priority ordering, timeouts, and captured output all surface here.</p>
            </div>
            <div class="badge">Live from SQLite</div>
          </header>
          <section class="cards">
            {cards}
          </section>
          <section class="panel">
            <h2>Recent Jobs</h2>
            {'' if job_rows else '<div class="empty">No jobs found.</div>'}
            <table>
              <thead>
                <tr>
                  <th>ID</th>
                  <th>State</th>
                  <th>Priority</th>
                  <th>Run At</th>
                  <th>Timeout</th>
                  <th>Attempts</th>
                  <th>Exit Code</th>
                </tr>
              </thead>
              <tbody id="recent-jobs-body">
                {''.join(job_rows)}
              </tbody>
            </table>
            <div class="links">
              <a href="/metrics">/metrics</a>
              <a href="/jobs">/jobs</a>
            </div>
          </section>
          {dashboard_script}
        </main>
      </body>
    </html>
    """


def _render_jobs_json() -> str:
    return json.dumps(_jobs_snapshot(limit=50), indent=2, sort_keys=True)


def _render_metrics_text() -> str:
    conn = _snapshot_connection()
    try:
        return format_metrics(conn)
    finally:
        conn.close()


def _metrics_snapshot() -> dict[str, int]:
    conn = _snapshot_connection()
    try:
        return collect_metrics(conn, refresh_workers=False)
    finally:
        conn.close()


def _jobs_snapshot(limit: int) -> list[dict[str, object]]:
    conn = _snapshot_connection()
    try:
        jobs = list_jobs(conn, refresh=False)
        return jobs[:limit]
    finally:
        conn.close()


def _snapshot_connection() -> sqlite3.Connection:
    conn = connect()
    initialize(conn)
    conn.commit()
    return conn
