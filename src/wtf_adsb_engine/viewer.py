"""Private factual viewer and an explicitly separate, immutable writing archive."""

import csv
import html
import io
import json
import logging
import math
import re
import signal
import sqlite3
import threading
import time
from contextlib import contextmanager, suppress
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .analysis import flightaware, registry_http, selection
from .analysis.app import CATEGORIES, RARITY_REFRESH, baseline, event_dict, meta, rarity_context

ROOT = Path(__file__).parent
LOG = logging.getLogger(__name__)
EXPORT_STALL_SECONDS = 300


@contextmanager
def connect(path, *, writable=False, immutable=False):
    uri = Path(path).resolve().as_uri() + ("?mode=rw" if writable else "?mode=ro")
    if immutable:
        uri += "&immutable=1"
    db = sqlite3.connect(uri, uri=True, timeout=15)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA temp_store=MEMORY")
        if not writable:
            db.execute("PRAGMA query_only=ON")
        with db:
            db.execute("BEGIN IMMEDIATE" if writable else "BEGIN")
            yield db
    finally:
        db.close()


def decode_note(row):
    if row is None:
        return None
    try:
        content = json.loads(row["content"])
        if not isinstance(content, dict):
            return None
        for key in ("summary", "why_interesting", "cannot_conclude"):
            if not isinstance(content.get(key), str):
                return None
        for key in ("known_facts", "possible_explanations", "next_checks"):
            if not isinstance(content.get(key), list) or any(
                not isinstance(item, str) for item in content[key]
            ):
                return None
        return {**content, "created_at": row["created_at"], "model": row["model"]}
    except ValueError, TypeError, KeyError:
        return None


def saved_note(db, event_id):
    return decode_note(
        db.execute("SELECT created_at,model,content FROM notes WHERE event_id=?", (event_id,)).fetchone()
    )


def decode_draft(row):
    if row is None:
        return None
    try:
        story, packet = json.loads(row["content"]), json.loads(row["evidence"])
        if not isinstance(story, dict) or not isinstance(packet, dict):
            return None
        for key in ("headline", "standfirst", "caveat"):
            if not isinstance(story.get(key), str):
                return None
        for key in ("paragraphs", "source_ids"):
            if not isinstance(story.get(key), list) or any(not isinstance(item, str) for item in story[key]):
                return None
        links = {link["id"]: link for link in packet["links"]}
        return {
            **story,
            "created_at": row["created_at"],
            "model": row["model"],
            "links": [links[source] for source in story["source_ids"] if source in links],
        }
    except ValueError, TypeError, KeyError:
        return None


def saved_draft(db, event_id):
    return decode_draft(
        db.execute(
            "SELECT created_at,model,content,evidence FROM story_drafts WHERE event_id=?", (event_id,)
        ).fetchone()
    )


def attach_writing(db, events):
    ids = [event["id"] for event in events]
    if not ids:
        return
    marks = ",".join("?" * len(ids))
    notes = {
        row["event_id"]: decode_note(row)
        for row in db.execute(
            f"SELECT event_id,created_at,model,content FROM notes WHERE event_id IN ({marks})", ids
        )
    }
    drafts = {
        row["event_id"]: decode_draft(row)
        for row in db.execute(
            f"SELECT event_id,created_at,model,content,evidence FROM story_drafts WHERE event_id IN ({marks})",
            ids,
        )
    }
    for event in events:
        event["note"] = notes.get(event["id"])
        draft = drafts.get(event["id"])
        event["draft_headline"] = draft.get("headline") if draft else None


def query_events(db, params, now, limit=120, stream=False):
    view = params.get("view", ["all"])[0]
    if view in {"promoted", "review", "encounters"}:
        return selection.select(db, params, now, limit, stream)
    days = params.get("days", ["1"])[0]
    days = int(days) if days in {"1", "7", "30", "365"} else 1
    clauses, args = ["last_seen>=?"], [now - days * 86400]
    if view == "drafts":
        clauses.append("id IN (SELECT event_id FROM story_drafts)")
    category = params.get("category", [""])[0]
    if category in CATEGORIES:
        clauses.append("category=?")
        args.append(category)
    search = params.get("q", [""])[0][:80].strip()
    if search:
        # Match the typed text literally; % and _ are not wildcards here.
        clauses.append("(hex LIKE ? ESCAPE '\\' OR title LIKE ? ESCAPE '\\' OR evidence LIKE ? ESCAPE '\\')")
        args.extend(["%" + re.sub(r"([\\%_])", r"\\\1", search) + "%"] * 3)
    where = " AND ".join(clauses)
    total = db.execute("SELECT COUNT(*) FROM events WHERE " + where, args).fetchone()[0]
    if params.get("sort", [""])[0] == "wow":
        query = (
            "WITH ranked AS (SELECT id,score,value,last_seen,ROW_NUMBER() OVER (PARTITION BY kind,COALESCE(NULLIF(json_extract(evidence,'$.aircraft.t'),''),hex) ORDER BY score DESC,value DESC,last_seen DESC) AS variety_rank FROM events WHERE "
            + where
            + ") SELECT e.* FROM events e JOIN ranked r ON r.id=e.id ORDER BY r.variety_rank ASC,r.score DESC,r.value DESC,r.last_seen DESC LIMIT ?"
        )
    else:
        query = "SELECT * FROM events WHERE " + where + " ORDER BY last_seen DESC,score DESC LIMIT ?"

    def decoded_events():
        for row in db.execute(query, (*args, limit)):
            event = event_dict(row)
            event["selection"] = {"verdict": selection.review_verdict(db, event)}
            yield event

    events = decoded_events()
    return (events if stream else list(events)), total


def json_export(db, events, total, now, *, archive=False):
    yield '{"exported_at":' + str(now) + ',"count":' + str(min(total, selection.EVENT_LIMIT)) + ',"events":['
    separator = ""
    for event in events:
        if archive:
            event["note"] = saved_note(db, event["id"])
            event["story"] = saved_draft(db, event["id"])
        yield separator + json.dumps(event, allow_nan=False)
        separator = ","
    yield "]}"


def csv_export(events):
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "first_seen_utc",
            "last_seen_utc",
            "category",
            "hex",
            "registration",
            "type",
            "callsign",
            "title",
            "value",
            "unit",
            "observations",
            "why",
            "interpretation",
            "origin",
        ]
    )
    yield output.getvalue()
    for event in events:
        output.seek(0)
        output.truncate(0)
        a = event["aircraft"]
        cells = [
            datetime.fromtimestamp(event[t], timezone.utc).isoformat() for t in ("first_seen", "last_seen")
        ]
        cells += [
            event["category"],
            event["hex"],
            a.get("r"),
            a.get("t"),
            a.get("flight"),
            event["title"],
            event["value"],
            event["unit"],
            event["hits"],
            event["why"],
            event["explanation"],
            event["origin"],
        ]
        writer.writerow(
            ["'" + str(v) if isinstance(v, str) and v.startswith(("=", "+", "-", "@")) else v for v in cells]
        )
        yield output.getvalue()


def summary(db, params, now, *, archive=False, settings=None, type_registry=False, wind=False):
    events, total = query_events(db, params, now)
    for event in [] if archive else events:
        flightaware.attach_context(
            db, event, now, fleet_reference_file=settings.fleet_reference_file if settings else None
        )
    if archive:
        attach_writing(db, events)
    # The collector saves these each time it reads the rarity baseline. Compute
    # them here only when it has not run recently, such as on a new journal.
    slow = meta(db, "baseline")
    try:
        fresh = 0 <= now - slow["at"] <= 5 * RARITY_REFRESH and {"rarity", "types", "records"} <= set(slow)
    except TypeError, KeyError:
        # A missing row, or one saved in another shape by a different version.
        fresh = False
    if not fresh:
        slow = baseline(db, rarity_context(db, now))
    counts = dict(
        db.execute(
            "SELECT category,COUNT(*) FROM events WHERE last_seen>=? GROUP BY category", (now - 86400,)
        )
    )
    last_success = meta(db, "last_success", 0)
    timeline = [
        dict(row)
        for row in db.execute(
            "SELECT CAST(first_seen/3600 AS INTEGER)*3600 AS hour,category,COUNT(*) AS count FROM events WHERE first_seen>=? GROUP BY hour,category ORDER BY hour",
            (now - 86400,),
        )
    ]
    return {
        "now": now,
        "last_success": last_success,
        "healthy": archive or 0 <= now - last_success < 120,
        "archive": archive,
        "captured_at": now if archive else None,
        "error": meta(db, "last_error"),
        "installed_at": meta(db, "installed_at"),
        "history_import": meta(db, "history_imported"),
        "live_aircraft": meta(db, "live_aircraft", 0),
        "flightaware": {"enabled": False, "status": "Saved evidence only"}
        if archive
        else flightaware.status(db, now, settings),
        "type_check": registry_http.status(
            db, "typecheck", type_registry, "Waiting for aircraft type checks"
        ),
        "wind": registry_http.status(db, "wind", wind, "Waiting for wind checks"),
        "metadata_gaps": meta(db, "metadata_gaps", 0),
        "unique_ids": db.execute("SELECT COUNT(*) FROM aircraft").fetchone()[0],
        "event_count_24h": sum(counts.values()),
        "categories_24h": counts,
        "events": events,
        "matching_events": total,
        "types": slow["types"],
        "records": slow["records"],
        "rarity": slow["rarity"],
        "timeline": timeline,
        "archive_scan": meta(db, "archive_scan"),
        "ai": {
            "enabled": False,
            "mode": "archive",
            "status": "Read-only saved writing" if archive else "Paid writing runs in the publisher",
            "notes": db.execute("SELECT COUNT(*) FROM notes").fetchone()[0] if archive else 0,
            "drafts": db.execute("SELECT COUNT(*) FROM story_drafts").fetchone()[0] if archive else 0,
        },
    }


class Handler(BaseHTTPRequestHandler):
    # A stalled client must not hold one of the container's limited threads forever.
    timeout = 30
    responded = False

    def log_message(self, format, *args):
        pass

    def end_headers(self):
        super().end_headers()
        self.responded = True

    def respond(self, action):
        try:
            action()
        except BrokenPipeError, ConnectionResetError, TimeoutError:
            pass
        except Exception as error:
            LOG.warning("Request failed (%s)", type(error).__name__)
            # A response that has started cannot change its status.
            if not self.responded:
                with suppress(OSError):
                    self.send_json(500, {"error": "The journal is temporarily unavailable."})

    def do_GET(self):
        self.respond(self.route)

    def do_POST(self):
        self.respond(self.feedback)

    def feedback(self):
        if self.path.startswith("/wtf/api/archive/"):
            self.send_json(405, {"error": "The writing archive is read-only"})
            return
        if self.path != "/wtf/api/feedback":
            self.send_json(404, {"error": "Not found"})
            return
        # Private Tailnet endpoint. Require a same-origin JSON request; forms and
        # cross-site browser requests cannot alter the review labels.
        try:
            origin = urlsplit(self.headers.get("Origin", ""))
            if (
                origin.scheme not in {"http", "https"}
                or origin.netloc != self.headers.get("Host")
                or self.headers.get("Sec-Fetch-Site", "same-origin") != "same-origin"
            ):
                status, answer = 403, {"error": "Same-origin review required"}
            else:
                if self.headers.get("Content-Type", "").split(";", 1)[0] != "application/json":
                    raise ValueError("JSON required")
                length = int(self.headers.get("Content-Length", "0"))
                if not 1 <= length <= 4096 or self.headers.get("Transfer-Encoding"):
                    raise ValueError("Invalid request length")
                self.connection.settimeout(5)
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict) or set(body) != {"event_id", "verdict"}:
                    raise ValueError("Invalid review fields")
                with connect(self.server.journal, writable=True) as db:
                    selection.save_review(db, body["event_id"], body["verdict"], time.time())
                status, answer = 200, {"saved": True}
        except ValueError, TypeError, UnicodeError, TimeoutError:
            status, answer = 400, {"error": "Invalid review request"}
        # Answer outside the try: a failed write must not be followed by a second response.
        self.send_json(status, answer)

    def send_stream(self, chunks, mime, filename):
        # HTTP/1.0 close-delimited export keeps a full year of evidence out of RAM.
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Disposition", 'attachment; filename="' + filename + '"')
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        # A reader may pause far longer on a large download than on a page.
        self.connection.settimeout(EXPORT_STALL_SECONDS)
        try:
            for chunk in chunks:
                self.wfile.write(chunk.encode("utf-8"))
        except OSError as error:
            # A close-delimited download cannot tell its reader that it is incomplete,
            # and respond() treats a dropped or stalled connection as routine.
            LOG.warning("Export interrupted (%s)", type(error).__name__)
            raise

    def send(self, status, body, mime="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'self'; base-uri 'self'; form-action 'self'",
        )
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, status, value):
        self.send(status, json.dumps(value, allow_nan=False))

    def route(self):
        try:
            url = urlsplit(self.path)
            params = parse_qs(url.query, max_num_fields=32)
        except ValueError:
            self.send_json(400, {"error": "Invalid request URL or too many query fields"})
            return
        if url.path in {"/wtf", "/"}:
            self.send_response(302)
            self.send_header("Location", "/wtf/")
            self.end_headers()
            return
        static = {
            "/wtf/": ("index.html", "text/html; charset=utf-8"),
            "/wtf/archive/": ("index.html", "text/html; charset=utf-8"),
            "/wtf/app.js": ("app.js", "application/javascript; charset=utf-8"),
            "/wtf/style.css": ("style.css", "text/css; charset=utf-8"),
            "/wtf/favicon.svg": ("favicon.svg", "image/svg+xml"),
        }
        if url.path in static:
            filename, mime = static[url.path]
            content = (ROOT / "static" / filename).read_bytes()
            if filename == "index.html":
                content = content.replace(
                    b"<body>",
                    (
                        '<body data-map-url="' + html.escape(self.server.map_url or "", quote=True) + '">'
                    ).encode(),
                )
            self.send(200, content, mime)
            return
        if url.path == "/wtf/health":
            self.send_json(200, {"alive": True})
            return
        archive = url.path.startswith("/wtf/api/archive/")
        if archive and self.server.archive is None:
            self.send_json(404, {"error": "No writing archive is configured"})
            return
        path = url.path.replace("/wtf/api/archive/", "/wtf/api/", 1) if archive else url.path
        if archive and path.endswith("/feedback"):
            self.send_json(405, {"error": "The writing archive is read-only"})
            return
        if not archive and params.get("view", [""])[0] == "drafts":
            self.send_json(400, {"error": "Saved drafts belong to the archive"})
            return
        now = self.server.captured_at if archive else time.time()
        with connect(self.server.archive if archive else self.server.journal, immutable=archive) as db:
            if path == "/wtf/api/summary":
                self.send_json(
                    200,
                    summary(
                        db,
                        params,
                        now,
                        archive=archive,
                        settings=self.server.settings,
                        type_registry=self.server.type_registry,
                        wind=self.server.wind,
                    ),
                )
            elif re.fullmatch(r"/wtf/api/event/[0-9]+", path):
                identifier = path.rsplit("/", 1)[-1]
                if len(identifier) > 19 or not 1 <= int(identifier) <= 9223372036854775807:
                    self.send_json(400, {"error": "Invalid sighting ID"})
                    return
                row = db.execute("SELECT * FROM events WHERE id=?", (int(identifier),)).fetchone()
                if not row:
                    self.send_json(404, {"error": "Sighting not found"})
                    return
                event = event_dict(row)
                if not archive:
                    flightaware.attach_context(
                        db, event, now, fleet_reference_file=self.server.settings.fleet_reference_file
                    )
                event["verdict"] = selection.review_verdict(db, event)
                profiles = selection.performance_profiles(db)
                event["selection"] = selection.assess(
                    event, profiles, independent=selection.independent_finding(db, event, profiles)
                )
                event["note"] = saved_note(db, event["id"]) if archive else None
                event["story"] = saved_draft(db, event["id"]) if archive else None
                self.send_json(200, event)
            elif path == "/wtf/api/export.json":
                events, total = query_events(db, params, now, selection.EVENT_LIMIT, stream=True)
                self.send_stream(
                    json_export(db, events, total, now, archive=archive),
                    "application/json; charset=utf-8",
                    "wtf-radar-sightings.json",
                )
            elif path == "/wtf/api/export.csv":
                events, _ = query_events(db, params, now, selection.EVENT_LIMIT, stream=True)
                self.send_stream(csv_export(events), "text/csv; charset=utf-8", "wtf-radar-sightings.csv")
            elif path == "/wtf/metrics":
                counts = dict(
                    db.execute(
                        "SELECT category,COUNT(*) FROM events WHERE last_seen>=? GROUP BY category",
                        (now - 86400,),
                    )
                )
                lines = ["# TYPE adsb_discoveries_sightings_24h gauge"]
                lines += [
                    f'adsb_discoveries_sightings_24h{{category="{c}"}} {counts.get(c, 0)}'
                    for c in sorted(CATEGORIES)
                ]
                lines += [
                    "# TYPE adsb_discoveries_last_success_seconds gauge",
                    f"adsb_discoveries_last_success_seconds {meta(db, 'last_success', 0)}",
                    "# TYPE adsb_discoveries_unique_ids gauge",
                    f"adsb_discoveries_unique_ids {db.execute('SELECT COUNT(*) FROM aircraft').fetchone()[0]}",
                ]
                self.send(200, "\n".join(lines) + "\n", "text/plain; version=0.0.4; charset=utf-8")
            else:
                self.send_json(404, {"error": "Not found"})


class ViewerServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address,
        journal,
        *,
        archive=None,
        captured_at=None,
        settings=None,
        type_registry=False,
        wind=False,
        map_url=None,
    ):
        from .maintenance import journal_info

        journal_info(journal)
        self.journal = Path(journal)
        self.archive = Path(archive) if archive else None
        if self.archive:
            if self.archive.resolve() == self.journal.resolve() or self.archive.samefile(self.journal):
                raise ValueError("The writing archive must be separate from the active journal")
            if type(captured_at) not in (float, int) or not math.isfinite(captured_at) or captured_at <= 0:
                raise ValueError("An archive requires its fixed capture timestamp")
            if any(Path(str(self.archive) + suffix).exists() for suffix in ("-wal", "-journal")):
                raise ValueError("The archive must be a standalone SQLite backup")
            if not {"notes", "story_drafts"} <= set(journal_info(self.archive)["tables"]):
                raise ValueError("The archive is missing saved writing tables")
        self.captured_at = captured_at
        self.settings = settings or flightaware.Settings()
        self.type_registry, self.wind = type_registry, wind
        if map_url is not None and not (
            map_url.startswith("/")
            and not map_url.startswith("//")
            or urlsplit(map_url).scheme in {"http", "https"}
        ):
            raise ValueError("Map links require a relative path or HTTP(S) URL prefix")
        self.map_url = map_url
        super().__init__(address, Handler)


def serve(
    config_path, *, pipeline=None, archive=None, captured_at=None, host="127.0.0.1", port=8080, map_url=None
):
    from .maintenance import native_journal
    from .runtime import configuration

    config = configuration(config_path)
    journal = native_journal(config, pipeline)
    selected = next(item for item in config["pipelines"] if journal.parts[-3] == item["name"])
    options = selected["input"].get("options", {})
    settings = flightaware.Settings(
        enabled=options.get("flightaware", False),
        pause_file=options.get("flightaware_pause_file"),
        daily_budget=flightaware.money(options.get("flightaware_daily_budget_usd", 0.50)),
        monthly_budget=flightaware.money(options.get("flightaware_monthly_budget_usd", 10)),
        fleet_reference_file=options.get("fleet_reference_file"),
    )
    with ViewerServer(
        (host, port),
        journal,
        archive=archive,
        captured_at=captured_at,
        settings=settings,
        type_registry=options.get("type_registry", False),
        wind=options.get("wind", False),
        map_url=map_url,
    ) as server:
        stop = threading.Event()
        for signum in (signal.SIGTERM, signal.SIGINT):
            signal.signal(signum, lambda *_, stop=stop: stop.set())
        server.timeout = 0.5
        while not stop.is_set():
            server.handle_request()
