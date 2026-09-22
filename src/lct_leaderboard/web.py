import cgi
import csv
import html
import io
import json
import os
import sqlite3
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlencode, urlparse

from .catalog import RuleCatalog
from .io import read_bytes, read_json
from .scoring import SubmissionScoreInput, score_dataset
from .validator import validate_result_geojson


APP_TITLE = "LCT Heat Network Leaderboard"


class AppState:
    def __init__(self) -> None:
        self.data_dir = Path(os.environ.get("LCT_DATA_DIR", "data"))
        self.upload_dir = self.data_dir / "uploads"
        self.db_path = self.data_dir / "leaderboard.sqlite"
        self.max_upload_bytes = int(os.environ.get("LCT_MAX_UPLOAD_BYTES", "25000000"))
        self.max_submissions_per_team = int(
            os.environ.get("LCT_MAX_SUBMISSIONS_PER_TEAM_DATASET", "3")
        )
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        _init_db(self.db_path)


class LeaderboardHandler(BaseHTTPRequestHandler):
    state: AppState

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/healthz":
            self._send_json({"ok": True, "configured": _config_status()["ready"]})
            return
        if parsed.path.startswith("/download/"):
            self._serve_download(parsed.path)
            return
        if parsed.path == "/export.csv":
            self._serve_export(parsed.query)
            return
        if parsed.path == "/submission":
            self._serve_submission_report(parsed.query)
            return
        if parsed.path != "/":
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        params = parse_qs(parsed.query)
        dataset_id = _first(params, "dataset_id") or "new_tz_smoke"
        submissions = _list_submissions(self.state.db_path, dataset_id=dataset_id)
        leaderboard = _leaderboard_for_dataset(submissions, dataset_id)
        body = _render_page(
            dataset_id=dataset_id,
            submissions=submissions,
            leaderboard=leaderboard,
            config_status=_config_status(),
            message=_first(params, "message"),
            error=_first(params, "error"),
        )
        self._send_html(body)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/submit":
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        dataset_id = "new_tz_smoke"
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
            if content_length > self.state.max_upload_bytes:
                raise ValueError("Файл слишком большой.")

            form = cgi.FieldStorage(
                fp=self.rfile,
                headers=self.headers,
                environ={
                    "REQUEST_METHOD": "POST",
                    "CONTENT_TYPE": self.headers.get("Content-Type", ""),
                    "CONTENT_LENGTH": str(content_length),
                },
            )
            team_id = _form_value(form, "team_id").strip()
            dataset_id = _form_value(form, "dataset_id").strip() or dataset_id
            upload = form["result_file"] if "result_file" in form else None

            team_id = _normalize_team_id(team_id)
            if _submissions_closed():
                raise ValueError("Прием сабмитов закрыт.")
            if upload is None or not getattr(upload, "filename", ""):
                raise ValueError("Загрузите result.geojson.")
            if not _looks_like_geojson_filename(upload.filename):
                raise ValueError("Файл результата должен быть .geojson или .json.")
            if (
                _submission_count(self.state.db_path, team_id, dataset_id)
                >= self.state.max_submissions_per_team
            ):
                raise ValueError(
                    "Лимит сабмитов для этой команды и датасета уже исчерпан."
                )

            catalog = RuleCatalog.from_dict(read_json(_required_env("LCT_CATALOG_PATH")))
            input_geojson = _optional_input_geojson()
            raw = upload.file.read()
            result_geojson = json.loads(raw.decode("utf-8-sig"))
            report = validate_result_geojson(result_geojson, catalog, input_geojson)
            submission_id = _insert_submission(
                db_path=self.state.db_path,
                upload_dir=self.state.upload_dir,
                team_id=team_id,
                dataset_id=dataset_id,
                original_filename=upload.filename,
                result_geojson=result_geojson,
                report=report.to_dict(),
            )
            self._redirect(
                "/?"
                + urlencode(
                    {
                        "dataset_id": dataset_id,
                        "message": f"Сабмит #{submission_id} обработан.",
                    }
                )
            )
        except Exception as exc:
            self._redirect("/?" + urlencode({"dataset_id": dataset_id, "error": str(exc)}))

    def log_message(self, format: str, *args: Any) -> None:
        print("[%s] %s" % (self.log_date_time_string(), format % args))

    def _send_html(self, body: str) -> None:
        payload = body.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _send_json(self, data: Dict[str, Any]) -> None:
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _send_csv(self, text: str, filename: str) -> None:
        payload = text.encode("utf-8-sig")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header(
            "Content-Disposition",
            f'attachment; filename="{_safe_name(filename)}"',
        )
        self.end_headers()
        self.wfile.write(payload)

    def _send_bytes(self, payload: bytes, filename: str, content_type: str) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header(
            "Content-Disposition",
            f'attachment; filename="{_safe_name(filename)}"',
        )
        self.end_headers()
        self.wfile.write(payload)

    def _serve_download(self, path: str) -> None:
        downloads = _download_registry()
        item = downloads.get(path)
        if item is None:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        source_path = item.get("path")
        if not source_path:
            self.send_error(HTTPStatus.NOT_FOUND, "Download is not configured")
            return
        try:
            self._send_bytes(
                read_bytes(source_path),
                filename=item["filename"],
                content_type=item["content_type"],
            )
        except FileNotFoundError:
            self.send_error(HTTPStatus.NOT_FOUND, "Configured file was not found")

    def _serve_export(self, query: str) -> None:
        params = parse_qs(query)
        dataset_id = _first(params, "dataset_id") or "new_tz_smoke"
        submissions = _list_submissions(self.state.db_path, dataset_id=dataset_id)
        leaderboard = _leaderboard_for_dataset(submissions, dataset_id)
        self._send_csv(
            _leaderboard_csv(leaderboard),
            filename=f"leaderboard_{dataset_id}.csv",
        )

    def _serve_submission_report(self, query: str) -> None:
        params = parse_qs(query)
        submission_id = _first(params, "id")
        if submission_id is None or not submission_id.isdigit():
            self.send_error(HTTPStatus.BAD_REQUEST, "Invalid submission id")
            return
        report = _submission_report(self.state.db_path, int(submission_id))
        if report is None:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self._send_json(report)

    def _redirect(self, location: str) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self.end_headers()


def main() -> None:
    port = int(os.environ.get("PORT", "8000"))
    state = AppState()
    handler = type("ConfiguredLeaderboardHandler", (LeaderboardHandler,), {"state": state})
    server = ThreadingHTTPServer(("0.0.0.0", port), handler)
    print(f"{APP_TITLE} listening on http://localhost:{port}")
    server.serve_forever()


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Не задана переменная окружения {name}.")
    return value


def _optional_input_geojson() -> Optional[Dict[str, Any]]:
    input_path = os.environ.get("LCT_INPUT_PATH")
    if not input_path:
        return None
    return read_json(input_path)


def _config_status() -> Dict[str, Any]:
    catalog_path = os.environ.get("LCT_CATALOG_PATH")
    input_path = os.environ.get("LCT_INPUT_PATH")
    sample_result_path = os.environ.get("LCT_SAMPLE_RESULT_PATH")
    return {
        "ready": bool(catalog_path),
        "submissions_closed": _submissions_closed(),
        "submissions_close_at": os.environ.get("LCT_SUBMISSIONS_CLOSE_AT", ""),
        "max_submissions_per_team": int(
            os.environ.get("LCT_MAX_SUBMISSIONS_PER_TEAM_DATASET", "3")
        ),
        "max_upload_mb": round(
            int(os.environ.get("LCT_MAX_UPLOAD_BYTES", "25000000")) / 1_000_000,
            1,
        ),
        "downloads": list(_download_registry().values()),
    }


def _download_registry() -> Dict[str, Dict[str, str]]:
    return {
        "/download/input": {
            "label": "Input dataset",
            "path": os.environ.get("LCT_INPUT_PATH", ""),
            "filename": "input.geojson",
            "content_type": "application/geo+json; charset=utf-8",
        },
        "/download/catalog": {
            "label": "Rule catalog",
            "path": os.environ.get("LCT_CATALOG_PATH", ""),
            "filename": "rule_catalog.json",
            "content_type": "application/json; charset=utf-8",
        },
        "/download/sample-result": {
            "label": "Sample result",
            "path": os.environ.get("LCT_SAMPLE_RESULT_PATH", ""),
            "filename": "valid_result.geojson",
            "content_type": "application/geo+json; charset=utf-8",
        },
        "/download/examples": {
            "label": "30 example datasets",
            "path": os.environ.get("LCT_EXAMPLES_ZIP_PATH", ""),
            "filename": "examples_30.zip",
            "content_type": "application/zip",
        },
    }


def _connect(db_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(db_path))
    connection.row_factory = sqlite3.Row
    return connection


def _init_db(db_path: Path) -> None:
    with _connect(db_path) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS submissions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                team_id TEXT NOT NULL,
                dataset_id TEXT NOT NULL,
                accepted INTEGER NOT NULL,
                status TEXT NOT NULL,
                calculated_cost REAL NOT NULL,
                new_network_length REAL NOT NULL,
                basic_score REAL,
                issues_json TEXT NOT NULL,
                report_json TEXT NOT NULL,
                result_path TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_submissions_dataset ON submissions(dataset_id)"
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_submissions_team_dataset
            ON submissions(team_id, dataset_id)
            """
        )


def _insert_submission(
    db_path: Path,
    upload_dir: Path,
    team_id: str,
    dataset_id: str,
    original_filename: str,
    result_geojson: Dict[str, Any],
    report: Dict[str, Any],
) -> int:
    metrics = report.get("metrics", {})
    created_at = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    with _connect(db_path) as connection:
        cursor = connection.execute(
            """
            INSERT INTO submissions (
                team_id,
                dataset_id,
                accepted,
                status,
                calculated_cost,
                new_network_length,
                basic_score,
                issues_json,
                report_json,
                result_path,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                team_id,
                dataset_id,
                1 if report.get("accepted") else 0,
                report.get("status", "failed"),
                float(metrics.get("calculated_cost") or 0.0),
                float(metrics.get("new_network_length") or 0.0),
                metrics.get("basic_score"),
                json.dumps(report.get("issues", []), ensure_ascii=False),
                json.dumps(report, ensure_ascii=False),
                "",
                created_at,
            ),
        )
        submission_id = int(cursor.lastrowid)
        result_path = upload_dir / f"{submission_id}_{_safe_name(original_filename)}"
        result_path.write_text(
            json.dumps(result_geojson, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        connection.execute(
            "UPDATE submissions SET result_path = ? WHERE id = ?",
            (str(result_path), submission_id),
        )
        return submission_id


def _safe_name(value: str) -> str:
    allowed = []
    for char in value:
        if char.isalnum() or char in {".", "_", "-"}:
            allowed.append(char)
        else:
            allowed.append("_")
    return "".join(allowed) or "result.geojson"


def _list_submissions(db_path: Path, dataset_id: str) -> List[Dict[str, Any]]:
    with _connect(db_path) as connection:
        rows = connection.execute(
            """
            SELECT *
            FROM submissions
            WHERE dataset_id = ?
            ORDER BY created_at DESC, id DESC
            LIMIT 100
            """,
            (dataset_id,),
        ).fetchall()
    return [_row_to_submission(row) for row in rows]


def _submission_count(db_path: Path, team_id: str, dataset_id: str) -> int:
    with _connect(db_path) as connection:
        row = connection.execute(
            """
            SELECT COUNT(*) AS count
            FROM submissions
            WHERE team_id = ? AND dataset_id = ?
            """,
            (team_id, dataset_id),
        ).fetchone()
    return int(row["count"])


def _submission_report(db_path: Path, submission_id: int) -> Optional[Dict[str, Any]]:
    with _connect(db_path) as connection:
        row = connection.execute(
            """
            SELECT id, team_id, dataset_id, status, accepted, report_json, created_at
            FROM submissions
            WHERE id = ?
            """,
            (submission_id,),
        ).fetchone()
    if row is None:
        return None
    report = json.loads(row["report_json"])
    report["submission"] = {
        "id": row["id"],
        "team_id": row["team_id"],
        "dataset_id": row["dataset_id"],
        "status": row["status"],
        "accepted": bool(row["accepted"]),
        "created_at": row["created_at"],
    }
    return report


def _row_to_submission(row: sqlite3.Row) -> Dict[str, Any]:
    issues = json.loads(row["issues_json"])
    return {
        "id": row["id"],
        "team_id": row["team_id"],
        "dataset_id": row["dataset_id"],
        "accepted": bool(row["accepted"]),
        "status": row["status"],
        "calculated_cost": row["calculated_cost"],
        "new_network_length": row["new_network_length"],
        "basic_score": row["basic_score"],
        "issue_count": len(issues),
        "critical_count": sum(1 for issue in issues if issue.get("severity") == "critical"),
        "warning_count": sum(1 for issue in issues if issue.get("severity") == "warning"),
        "created_at": row["created_at"],
    }


def _leaderboard_for_dataset(
    submissions: List[Dict[str, Any]], dataset_id: str
) -> List[Dict[str, Any]]:
    best_by_team: Dict[str, Dict[str, Any]] = {}
    for submission in submissions:
        team_id = submission["team_id"]
        current = best_by_team.get(team_id)
        if current is None or _variant_key(submission) < _variant_key(current):
            best_by_team[team_id] = submission

    score_inputs = [
        SubmissionScoreInput(
            team_id=row["team_id"],
            dataset_id=dataset_id,
            accepted=row["accepted"],
            cost=row["calculated_cost"],
            length=row["new_network_length"],
        )
        for row in best_by_team.values()
    ]
    scores = score_dataset(score_inputs)
    by_team = {row.team_id: row for row in scores}
    rows = []
    for team_id, submission in best_by_team.items():
        score = by_team[team_id]
        rows.append(
            {
                "team_id": team_id,
                "submission_id": submission["id"],
                "accepted": submission["accepted"],
                "leaderboard_score": score.score,
                "calculated_cost": submission["calculated_cost"],
                "new_network_length": submission["new_network_length"],
                "warning_count": submission["warning_count"],
            }
        )
    rows.sort(
        key=lambda item: (
            -item["leaderboard_score"],
            item["calculated_cost"],
            item["new_network_length"],
            item["team_id"],
        )
    )
    for index, row in enumerate(rows, start=1):
        row["rank"] = index
    return rows


def _variant_key(submission: Dict[str, Any]) -> tuple:
    if not submission["accepted"]:
        return (1, submission["id"])
    return (
        0,
        0.7 * submission["calculated_cost"] / 25_000_000
        + 0.3 * submission["new_network_length"] / 100,
        submission["id"],
    )


def _form_value(form: cgi.FieldStorage, name: str) -> str:
    if name not in form:
        return ""
    field = form[name]
    if isinstance(field, list):
        field = field[0]
    return field.value if isinstance(field.value, str) else ""


def _first(params: Dict[str, List[str]], name: str) -> Optional[str]:
    values = params.get(name)
    return values[0] if values else None


def _normalize_team_id(value: str) -> str:
    normalized = " ".join(value.strip().split())
    if not normalized:
        raise ValueError("Укажите команду.")
    if len(normalized) > 64:
        raise ValueError("Название команды должно быть не длиннее 64 символов.")
    return normalized


def _looks_like_geojson_filename(filename: str) -> bool:
    lowered = filename.lower()
    return lowered.endswith(".geojson") or lowered.endswith(".json")


def _submissions_closed(now: Optional[datetime] = None) -> bool:
    raw_deadline = os.environ.get("LCT_SUBMISSIONS_CLOSE_AT", "").strip()
    if not raw_deadline:
        return False
    normalized = raw_deadline.replace("Z", "+00:00")
    deadline = datetime.fromisoformat(normalized)
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    return current >= deadline


def _e(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _money(value: float) -> str:
    return f"{value:,.0f}".replace(",", " ")


def _render_page(
    dataset_id: str,
    submissions: List[Dict[str, Any]],
    leaderboard: List[Dict[str, Any]],
    config_status: Dict[str, Any],
    message: Optional[str],
    error: Optional[str],
) -> str:
    leaderboard_rows = "\n".join(_render_leaderboard_row(row) for row in leaderboard)
    if not leaderboard_rows:
        leaderboard_rows = '<tr><td colspan="6" class="meta">Пока нет сабмитов.</td></tr>'

    submission_rows = "\n".join(_render_submission_row(row) for row in submissions)
    if not submission_rows:
        submission_rows = '<tr><td colspan="6" class="meta">История пуста.</td></tr>'

    message_html = f'<div class="notice ok">{_e(message)}</div>' if message else ""
    error_html = f'<div class="notice error">{_e(error)}</div>' if error else ""
    download_links = _render_download_links(config_status)
    disabled_attr = " disabled" if config_status.get("submissions_closed") else ""
    submit_label = "Submissions closed" if config_status.get("submissions_closed") else "Submit"

    return f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{_e(APP_TITLE)}</title>
  <style>{STYLE}</style>
</head>
<body>
  <header>
    <div>
      <h1>{_e(APP_TITLE)}</h1>
      <div class="meta">Dataset: {_e(dataset_id)}</div>
    </div>
    <div class="meta">Basic verifier · public demo</div>
  </header>
  <main>
    {error_html}
    {message_html}
    <div class="workspace">
      <div>
        <section>
          <div class="section-head"><h2>Leaderboard</h2><a class="inline-link" href="/export.csv?dataset_id={_e(dataset_id)}">Export CSV</a></div>
          <table>
            <thead><tr><th>Rank</th><th>Team</th><th class="number">Score</th><th class="number">Cost</th><th class="number">Length</th><th>Status</th></tr></thead>
            <tbody>{leaderboard_rows}</tbody>
          </table>
        </section>
        <section>
          <h2>Recent submissions</h2>
          <table>
            <thead><tr><th>ID</th><th>Team</th><th>Status</th><th class="number">Warnings</th><th class="number">Critical</th><th>Created</th></tr></thead>
            <tbody>{submission_rows}</tbody>
          </table>
        </section>
      </div>
      <aside>
        <section class="panel">
          <h2>Downloads</h2>
          <div class="download-list">{download_links}</div>
        </section>
        <section class="panel">
          <h2>Upload result</h2>
          <form method="post" action="/submit" enctype="multipart/form-data">
            <div class="field"><label for="team_id">Team</label><input id="team_id" name="team_id" autocomplete="organization" maxlength="64" required{disabled_attr}></div>
            <div class="field"><label for="dataset_id">Dataset</label><input id="dataset_id" name="dataset_id" value="{_e(dataset_id)}" required{disabled_attr}></div>
            <div class="field"><label for="result_file">GeoJSON result</label><input id="result_file" name="result_file" type="file" accept=".geojson,.json,application/json" required{disabled_attr}></div>
            <button type="submit"{disabled_attr}>{submit_label}</button>
          </form>
        </section>
        <section class="panel">
          <h2>Rules</h2>
          <div class="config">
            <div>Variants per team/dataset: {_e(config_status.get("max_submissions_per_team"))}</div>
            <div>Upload limit: {_e(config_status.get("max_upload_mb"))} MB</div>
            <div>Deadline: {_e(config_status.get("submissions_close_at") or "not set")}</div>
            <div>Status: {"closed" if config_status.get("submissions_closed") else "open"}</div>
          </div>
        </section>
      </aside>
    </div>
  </main>
</body>
</html>"""


def _render_leaderboard_row(row: Dict[str, Any]) -> str:
    status_class = "" if row["accepted"] else " bad"
    status = "accepted" if row["accepted"] else "rejected"
    return f"""<tr><td>{row["rank"]}</td><td>{_e(row["team_id"])}</td><td class="number">{row["leaderboard_score"]:.4f}</td><td class="number">{_money(row["calculated_cost"])}</td><td class="number">{row["new_network_length"]:.2f}</td><td><span class="status{status_class}">{status}</span></td></tr>"""


def _render_submission_row(row: Dict[str, Any]) -> str:
    status_class = "" if row["accepted"] else " bad"
    return f"""<tr><td><a class="inline-link" href="/submission?id={row["id"]}">#{row["id"]}</a></td><td>{_e(row["team_id"])}</td><td><span class="status{status_class}">{_e(row["status"])}</span></td><td class="number">{row["warning_count"]}</td><td class="number">{row["critical_count"]}</td><td>{_e(row["created_at"])}</td></tr>"""


def _leaderboard_csv(rows: List[Dict[str, Any]]) -> str:
    output = io.StringIO()
    writer = csv.DictWriter(
        output,
        fieldnames=[
            "rank",
            "team_id",
            "submission_id",
            "accepted",
            "leaderboard_score",
            "calculated_cost",
            "new_network_length",
            "warning_count",
        ],
    )
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return output.getvalue()


def _render_download_links(config_status: Dict[str, Any]) -> str:
    rows = []
    href_by_label = {
        "Input dataset": "/download/input",
        "Rule catalog": "/download/catalog",
        "Sample result": "/download/sample-result",
        "30 example datasets": "/download/examples",
    }
    for item in config_status.get("downloads", []):
        label = item["label"]
        href = href_by_label[label]
        if item.get("path"):
            rows.append(f'<a class="download-link" href="{href}">{_e(label)}</a>')
        else:
            rows.append(f'<span class="download-link disabled">{_e(label)} not configured</span>')
    return "\n".join(rows)


STYLE = """
:root { color-scheme: light; --bg: #f6f7f8; --ink: #111827; --muted: #667085; --line: #d9dee5; --panel: #ffffff; --accent: #0f766e; --bad: #b42318; --ok: #067647; }
* { box-sizing: border-box; }
body { margin: 0; font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; color: var(--ink); background: var(--bg); letter-spacing: 0; }
header { display: flex; align-items: center; justify-content: space-between; gap: 24px; padding: 22px 32px; border-bottom: 1px solid var(--line); background: rgba(255,255,255,.86); position: sticky; top: 0; backdrop-filter: blur(12px); z-index: 2; }
h1 { margin: 0; font-size: 20px; line-height: 1.2; font-weight: 760; }
main { max-width: 1240px; margin: 0 auto; padding: 28px 32px 56px; }
.workspace { display: grid; grid-template-columns: minmax(0, 1fr) 340px; gap: 28px; align-items: start; }
section { margin-bottom: 28px; }
h2 { margin: 0 0 14px; font-size: 15px; line-height: 1.3; font-weight: 720; }
.section-head { display: flex; align-items: center; justify-content: space-between; gap: 16px; margin-bottom: 14px; }
.section-head h2 { margin: 0; }
.inline-link { color: var(--accent); font-size: 13px; font-weight: 680; text-decoration: none; }
.inline-link:hover { text-decoration: underline; }
.meta { color: var(--muted); font-size: 13px; line-height: 1.45; }
.panel { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 18px; }
table { width: 100%; border-collapse: collapse; background: var(--panel); border: 1px solid var(--line); border-radius: 8px; overflow: hidden; font-size: 14px; }
th, td { padding: 12px 14px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: middle; white-space: nowrap; }
th { color: var(--muted); font-size: 12px; font-weight: 680; background: #fbfcfd; }
tr:last-child td { border-bottom: 0; }
.number { text-align: right; font-variant-numeric: tabular-nums; }
.status { display: inline-flex; align-items: center; min-height: 24px; padding: 0 8px; border-radius: 999px; font-size: 12px; font-weight: 680; background: #eef4f2; color: var(--ok); }
.status.bad { background: #fff1f0; color: var(--bad); }
label { display: block; margin: 0 0 6px; font-size: 12px; color: var(--muted); font-weight: 680; }
input { width: 100%; min-height: 40px; padding: 9px 10px; border: 1px solid var(--line); border-radius: 6px; background: white; color: var(--ink); font: inherit; }
input:focus { outline: 2px solid rgba(15,118,110,.18); border-color: var(--accent); }
.field { margin-bottom: 14px; }
button { width: 100%; min-height: 42px; border: 0; border-radius: 6px; background: var(--accent); color: white; font-weight: 760; cursor: pointer; }
button:disabled, input:disabled { cursor: not-allowed; opacity: .58; }
.notice { margin-bottom: 18px; padding: 12px 14px; border-radius: 8px; border: 1px solid var(--line); background: white; font-size: 14px; }
.notice.error { border-color: #fecdca; color: var(--bad); background: #fff7f6; }
.notice.ok { border-color: #abefc6; color: var(--ok); background: #f6fef9; }
.config { display: grid; gap: 7px; color: var(--muted); font-size: 12px; line-height: 1.45; overflow-wrap: anywhere; }
.download-list { display: grid; gap: 10px; }
.download-link { display: flex; align-items: center; justify-content: space-between; min-height: 38px; padding: 9px 10px; border: 1px solid var(--line); border-radius: 6px; color: var(--ink); text-decoration: none; background: #fff; font-size: 13px; font-weight: 680; }
.download-link::after { content: "Download"; color: var(--accent); font-size: 12px; }
.download-link.disabled { color: var(--muted); background: #f9fafb; }
.download-link.disabled::after { content: ""; }
@media (max-width: 860px) { header { align-items: flex-start; flex-direction: column; padding: 18px; } main { padding: 20px 16px 40px; } .workspace { grid-template-columns: 1fr; } table { display: block; overflow-x: auto; } }
"""


if __name__ == "__main__":
    main()
