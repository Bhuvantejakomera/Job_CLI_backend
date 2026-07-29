from __future__ import annotations

import json
import html
import logging
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
            parsed = urlparse(self.path)
            if parsed.path == "/":
                self._send_html(_render_dashboard())
            elif parsed.path == "/jobs":
                self._send_json(_render_jobs_json())
            elif parsed.path == "/metrics":
                self._send_text(_render_metrics_text())
            else:
                self.send_error(404, "Not found")

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

    return DashboardHandler


def _render_dashboard() -> str:
    metrics = _metrics_snapshot()
    jobs = _jobs_snapshot(limit=12)
    cards = "\n".join(
        f"""
        <article class="card">
          <div class="card-label">{html.escape(label)}</div>
          <div class="card-value">{value}</div>
        </article>
        """
        for label, value in (
            ("Pending", metrics["jobs_pending"]),
            ("Ready", metrics["jobs_pending_ready"]),
            ("Scheduled", metrics["jobs_pending_scheduled"]),
            ("Processing", metrics["jobs_processing"]),
            ("Completed", metrics["jobs_completed"]),
            ("Failed", metrics["jobs_failed"]),
            ("Dead", metrics["jobs_dead"]),
            ("Workers", metrics["workers_running"]),
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
              <tbody>
                {''.join(job_rows)}
              </tbody>
            </table>
            <div class="links">
              <a href="/metrics">/metrics</a>
              <a href="/jobs">/jobs</a>
            </div>
          </section>
        </main>
      </body>
    </html>
    """


def _render_jobs_json() -> str:
    return json.dumps(_jobs_snapshot(limit=50), indent=2, sort_keys=True)


def _render_metrics_text() -> str:
    conn = connect()
    initialize(conn)
    maintenance(conn)
    conn.commit()
    try:
        return format_metrics(conn)
    finally:
        conn.close()


def _metrics_snapshot() -> dict[str, int]:
    conn = connect()
    initialize(conn)
    maintenance(conn)
    conn.commit()
    try:
        return collect_metrics(conn)
    finally:
        conn.close()


def _jobs_snapshot(limit: int) -> list[dict[str, object]]:
    conn = connect()
    initialize(conn)
    maintenance(conn)
    conn.commit()
    try:
        jobs = list_jobs(conn)
        return jobs[:limit]
    finally:
        conn.close()
