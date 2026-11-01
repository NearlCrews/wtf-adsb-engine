"""Live feedback and immutable saved writing stay in separate namespaces."""

import hashlib
import http.client
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from wtf_adsb_engine import viewer
from wtf_adsb_engine.analysis import app, rules
from wtf_adsb_engine.maintenance import backup_journal

NOW = 1791150000


class ViewerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.active = self.root / "active.sqlite3"
        self.archive = self.root / "archive.sqlite3"
        app.initialize(self.active)
        with app.connect(self.active) as db:
            self.event_id = app.record(
                db,
                {"hex": "a12345", "r": "=FORMULA", "t": "C172", "gs": 1200, "type": "adsb_icao"},
                rules.Finding("outlier_gs", "oddities", "100% odd", 1200, "kn", "Review", "Review", 80),
                NOW,
                NOW,
            )
            db.execute(
                "CREATE TABLE notes(event_id INTEGER PRIMARY KEY,created_at REAL,model TEXT,content TEXT)"
            )
            db.execute(
                "CREATE TABLE story_drafts(event_id INTEGER PRIMARY KEY,created_at REAL,model TEXT,content TEXT,evidence TEXT)"
            )
            db.execute(
                "INSERT INTO notes VALUES (?,?,'mock',?)",
                (
                    self.event_id,
                    NOW,
                    json.dumps(
                        {
                            "summary": "Saved only in archive",
                            "why_interesting": "Synthetic",
                            "cannot_conclude": "Identity",
                            "known_facts": [],
                            "possible_explanations": [],
                            "next_checks": [],
                        }
                    ),
                ),
            )
            db.execute(
                "INSERT INTO story_drafts VALUES (?,?,'mock',?,?)",
                (
                    self.event_id,
                    NOW,
                    json.dumps(
                        {
                            "headline": "Saved draft",
                            "source_ids": [],
                            "standfirst": "Synthetic",
                            "caveat": "Synthetic",
                            "paragraphs": [],
                        }
                    ),
                    '{"links":[]}',
                ),
            )
        backup_journal(self.active, self.archive)
        with app.connect(self.active) as db:
            db.execute("DROP TABLE notes")
            db.execute("DROP TABLE story_drafts")
        self.server = viewer.ViewerServer(
            ("127.0.0.1", 0), self.active, archive=self.archive, captured_at=NOW + 60
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def request(self, path, *, payload=None, origin=True):
        client = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        headers = {"Content-Type": "application/json"}
        if origin:
            headers["Origin"] = "http://%s:%s" % self.server.server_address
        try:
            client.request(
                "POST" if payload is not None else "GET",
                path,
                json.dumps(payload) if payload is not None else None,
                headers,
            )
            response = client.getresponse()
            return response.status, response.read()
        finally:
            client.close()

    def test_active_without_writing_tables_and_old_archive_both_work(self):
        with patch.object(viewer.time, "time", return_value=NOW + 400 * 86400):
            code, raw = self.request("/wtf/api/summary?days=365")
            self.assertEqual(code, 200)
            self.assertEqual(json.loads(raw)["events"], [])
            code, raw = self.request("/wtf/api/archive/summary?days=365&view=drafts")
            self.assertEqual(code, 200)
            data = json.loads(raw)
            self.assertEqual(data["events"][0]["draft_headline"], "Saved draft")
            self.assertEqual(data["captured_at"], NOW + 60)
        code, raw = self.request(f"/wtf/api/event/{self.event_id}")
        self.assertEqual(code, 200)
        self.assertIsNone(json.loads(raw)["note"])
        code, raw = self.request(f"/wtf/api/archive/event/{self.event_id}")
        self.assertEqual(json.loads(raw)["note"]["summary"], "Saved only in archive")

    def test_archive_stays_immutable_and_feedback_is_active_only(self):
        before = hashlib.sha256(self.archive.read_bytes()).digest()
        payload = {"event_id": self.event_id, "verdict": "keep"}
        self.assertEqual(self.request("/wtf/api/archive/feedback", payload=payload)[0], 405)
        self.assertEqual(self.request("/wtf/api/feedback", payload=payload, origin=False)[0], 403)
        self.assertEqual(self.request("/wtf/api/feedback", payload=payload)[0], 200)
        self.request("/wtf/api/archive/export.json?days=365")
        self.request("/wtf/api/archive/summary?days=365")
        self.assertEqual(hashlib.sha256(self.archive.read_bytes()).digest(), before)
        self.assertFalse(Path(str(self.archive) + "-wal").exists())

    def test_search_literal_csv_escape_and_identifier_bounds(self):
        code, raw = self.request("/wtf/api/archive/summary?days=365&q=%25")
        self.assertEqual(json.loads(raw)["matching_events"], 1)
        code, raw = self.request("/wtf/api/archive/export.csv?days=365")
        self.assertEqual(code, 200)
        self.assertIn(b"'=FORMULA", raw)
        self.assertEqual(self.request("/wtf/api/event/" + "9" * 30)[0], 400)
        self.assertEqual(self.request("/wtf/health")[0], 200)
        self.assertIn(b"adsb_discoveries_unique_ids", self.request("/wtf/metrics")[1])

    def test_missing_journal_is_not_created(self):
        missing = self.root / "missing.sqlite3"
        with self.assertRaises(sqlite3.OperationalError):
            viewer.ViewerServer(("127.0.0.1", 0), missing)
        self.assertFalse(missing.exists())

    def test_malformed_saved_note_does_not_hide_factual_event(self):
        self.close()
        # Make a new immutable snapshot before restarting the archive server.
        with app.connect(self.archive) as db:
            db.execute("UPDATE notes SET content='not json'")
        with viewer.connect(self.archive) as db:
            self.assertIsNone(viewer.saved_note(db, self.event_id))
        # Cleanup can safely repeat shutdown/close after an explicit stop.

    def test_live_draft_query_cannot_be_hidden_by_repeated_parameters(self):
        self.assertEqual(self.request("/wtf/api/summary?view=drafts&view=all")[0], 400)

    def test_valid_json_with_wrong_saved_writing_shape_is_ignored(self):
        for value in ([], 42, "text", {"summary": []}, {"headline": 42}):
            with self.subTest(value=value):
                row = {"content": json.dumps(value), "evidence": "{}", "created_at": NOW, "model": "mock"}
                self.assertIsNone(viewer.decode_note(row))
                self.assertIsNone(viewer.decode_draft(row))


if __name__ == "__main__":
    unittest.main()
