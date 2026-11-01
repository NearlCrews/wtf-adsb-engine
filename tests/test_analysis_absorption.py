"""A verified circle or grid absorbs earlier pattern rows, on native and legacy journals.

Ported from the original application's OrbitPrecedenceTests (collector wiring).
Synthetic flights only (the episode geometry frame); no network or paid calls.
"""

import json
import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_analysis_episode_geometry import T0, Track, ll

from wtf_adsb_engine.analysis import app, export, rules, selection
from wtf_adsb_engine.analysis.native import NativeCollector

# Identities whose registration the US N-number algorithm assigns to the address.
LIGHT = {"r": "N8568S", "t": "C172", "category": "A1"}

# The original application's private writer tables, absent from the native journal.
LEGACY_TABLES = """
CREATE TABLE notes (event_id INTEGER PRIMARY KEY, created_at REAL NOT NULL,
  model TEXT NOT NULL, content TEXT NOT NULL, evidence TEXT NOT NULL);
CREATE TABLE story_drafts (event_id INTEGER PRIMARY KEY, created_at REAL NOT NULL,
  model TEXT NOT NULL, content TEXT NOT NULL, evidence TEXT NOT NULL);
"""


def stream(track, address, identity, *, period=10.0, wind=(0.0, 0.0)):
    """(time, aircraft.json report) pairs: an ADS-B position every ``period`` seconds."""
    states, index, messages, out = track.states, 0, 1000, []
    for t in sorted({round(i * period, 1) for i in range(int(states[-1][0] / period) + 1)}):
        while index + 1 < len(states) and states[index + 1][0] <= t:
            index += 1
        _, x, y, heading, altitude, speed, rate = states[index]
        x, y = x + wind[0] * t / 3600, y + wind[1] * t / 3600
        vx = speed * math.sin(math.radians(heading)) + wind[0]
        vy = speed * math.cos(math.radians(heading)) + wind[1]
        messages += 7
        lat, lon = ll(x, y)
        out.append(
            (
                round(T0 + t, 1),
                {
                    "hex": address,
                    **identity,
                    "alt_baro": round(altitude / 25) * 25,
                    "messages": messages,
                    "type": "adsb_icao",
                    "lat": lat,
                    "lon": lon,
                    "gs": round(math.hypot(vx, vy), 1),
                    "track": round(math.degrees(math.atan2(vx, vy)) % 360, 1),
                    "baro_rate": round(rate / 64) * 64,
                    "nic": 8,
                    "nac_p": 10,
                },
            )
        )
    return out


def run(process, streams, *, step=5.0, tail=0.0):
    """Poll like the collector: each snapshot holds every aircraft's latest report."""
    stamps = sorted({stamp for reports in streams.values() for stamp, _ in reports})
    position = {address: 0 for address in streams}
    latest, now = {}, stamps[0]
    with patch.object(app.watchlists, "lookup", return_value=None):
        while now <= stamps[-1] + tail + 1e-6:
            for address, reports in streams.items():
                while position[address] < len(reports) and reports[position[address]][0] <= now + 1e-6:
                    latest[address] = reports[position[address]]
                    position[address] += 1
            aircraft = []
            for stamp, report in latest.values():
                age = round(now - stamp, 1)
                if age <= 30:
                    aircraft.append({**report, "seen": age, **({"seen_pos": age} if "lat" in report else {})})
            process({"now": now, "aircraft": aircraft}, now)
            now = round(now + step, 1)


def interval(event):
    block = event["evidence"].get("maneuver") or event["evidence"].get("pattern") or {}
    return (
        block.get("turn_started_at", block.get("started_at")),
        block.get("turn_ended_at", block.get("ended_at")),
    )


def reversal_then_circle():
    """A reversal saved, then a circle that verifies over it."""
    track = Track(altitude=5000, speed=100).straight(150).turn(180, 3.0).straight(70)
    for _ in range(6):
        track.turn(360, 3.0)
    return track.straight(200)


def reversal_finding(start):
    finding = rules.Finding("course_reversal", "patterns", "Turn", 180, "degrees", "", "", 78, evidence={})
    finding.evidence = {"maneuver": {"turn_started_at": start + 60, "turn_ended_at": start + 120}}
    return finding


class JournalCase(unittest.TestCase):
    legacy = False

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "journal.sqlite3"
        app.initialize(self.path)
        if self.legacy:
            with app.connect(self.path) as db:
                db.executescript(LEGACY_TABLES)

    def events(self, kind=None):
        with app.connect(self.path) as db:
            rows = db.execute("SELECT * FROM events ORDER BY id").fetchall()
        return [app.event_dict(row) for row in rows if kind is None or row["kind"] == kind]

    def one(self, kind):
        found = self.events(kind)
        self.assertEqual(len(found), 1, [e["kind"] for e in self.events()])
        return found[0]


class NativeAbsorptionTests(JournalCase):
    def test_survey_grid_uses_retained_legs_and_absorbs_earlier_rows(self):
        track = Track(altitude=4500, speed=120, heading=90)
        for index in range(9):
            track.straight(150).turn(180 if index % 2 == 0 else -180, 3.0)
        track.straight(150)
        detector = app.Detector(self.path)
        run(detector.process, {"abc123": stream(track, "abc123", {**LIGHT, "t": "C208"})}, tail=120)
        grid = self.one("survey_grid")
        info = grid["evidence"]["maneuver"]
        # The grid spans more than the 20-minute buffer.
        self.assertGreater(info["ended_at"] - info["started_at"], 1800)
        self.assertGreaterEqual(info["leg_count"], 6)
        self.assertGreater(len(detector.legs["abc123"]["legs"]), 6)
        self.assertEqual(self.events("parallel_passes"), [])
        self.assertEqual(self.events("course_reversal"), [])

    def test_a_reversal_saved_before_the_circle_verified_is_absorbed(self):
        saved = []
        absorb = app.Detector._absorb

        def spy(detector, db, *args):
            saved.extend(db.execute("SELECT kind FROM events WHERE kind='course_reversal'").fetchall())
            return absorb(detector, db, *args)

        with patch.object(app.Detector, "_absorb", spy):
            run(
                app.Detector(self.path).process,
                {"abc123": stream(reversal_then_circle(), "abc123", LIGHT, wind=(5.0, 0.0))},
                tail=240,
            )
        self.assertTrue(saved, "the turn was saved as a reversal before the circle verified")
        self.assertEqual(self.events("course_reversal"), [])
        self.one("circling")

    def test_the_native_collector_records_the_circle_that_absorbs_a_saved_reversal(self):
        collector = NativeCollector(self.path)
        run(
            lambda snapshot, now: collector.process(snapshot, wall_now=now),
            {"abc123": stream(reversal_then_circle(), "abc123", LIGHT, wind=(5.0, 0.0))},
            tail=240,
        )
        self.assertEqual(self.events("course_reversal"), [])
        self.one("circling")
        self.assertEqual(collector.health()["detector_errors"], {})

    def test_an_absorbed_reversal_with_a_saved_review_is_marked_instead(self):
        with app.connect(self.path) as db:
            reversal = app.record(
                db,
                {"hex": "abc123"},
                reversal_finding(T0 + 40),
                T0 + 40,
                T0 + 220,
                extra={"encounter_start": T0},
            )
            selection.save_review(db, reversal, "keep", T0 + 300)
            circle = rules.Finding("circling", "patterns", "Circle", 3, "turns", "", "", 73, evidence={})
            circle.evidence["pattern"] = {"started_at": T0 + 90, "ended_at": T0 + 900}
            app.Detector(self.path)._absorb(db, "abc123", T0, circle, 99, T0 + 900)
            evidence = json.loads(
                db.execute("SELECT evidence FROM events WHERE id=?", (reversal,)).fetchone()[0]
            )
        self.assertEqual(evidence["absorbed_by"], {"kind": "circling", "event_id": 99})

    def test_an_absorbed_row_old_enough_to_be_published_is_marked_and_never_competes(self):
        circle = rules.Finding("circling", "patterns", "Circle", 3, "turns", "", "", 73, evidence={})
        circle.evidence["pattern"] = {"started_at": T0, "ended_at": T0 + 7200}
        with app.connect(self.path) as db:
            old, recent = (
                app.record(
                    db,
                    {"hex": "abc123"},
                    reversal_finding(start),
                    start,
                    start + 180,
                    extra={"encounter_start": T0},
                )
                for start in (T0, T0 + 6000)
            )
            # The publisher may already have released the old row; the recent one cannot be.
            app.Detector(self.path)._absorb(db, "abc123", T0, circle, 99, T0 + 7200)
            rows = dict(db.execute("SELECT id,evidence FROM events").fetchall())
        self.assertEqual(json.loads(rows[old])["absorbed_by"], {"kind": "circling", "event_id": 99})
        self.assertNotIn(recent, rows)
        with app.connect(self.path) as db:
            groups = selection.encounters(db, T0 + 7200)
        self.assertNotIn(old, [event["id"] for group in groups for event in group["events"]])


class LegacyAbsorptionTests(JournalCase):
    """The original application's journal keeps notes and drafts beside reviews."""

    legacy = True

    def test_an_absorbed_reversal_with_a_saved_note_is_marked_instead(self):
        with app.connect(self.path) as db:
            reversal = app.record(
                db,
                {"hex": "abc123"},
                reversal_finding(T0 + 40),
                T0 + 40,
                T0 + 220,
                extra={"encounter_start": T0},
            )
            db.execute("INSERT INTO notes VALUES (?,?,'mock','{}','{}')", (reversal, T0))
            circle = rules.Finding("circling", "patterns", "Circle", 3, "turns", "", "", 73, evidence={})
            circle.evidence["pattern"] = {"started_at": T0 + 90, "ended_at": T0 + 900}
            app.Detector(self.path)._absorb(db, "abc123", T0, circle, 99, T0 + 900)
            evidence = json.loads(
                db.execute("SELECT evidence FROM events WHERE id=?", (reversal,)).fetchone()[0]
            )
        self.assertEqual(evidence["absorbed_by"], {"kind": "circling", "event_id": 99})

    def test_an_absorbed_reversal_with_a_saved_draft_is_marked_instead(self):
        with app.connect(self.path) as db:
            reversal = app.record(
                db,
                {"hex": "abc123"},
                reversal_finding(T0 + 40),
                T0 + 40,
                T0 + 220,
                extra={"encounter_start": T0},
            )
            db.execute("INSERT INTO story_drafts VALUES (?,?,'mock','{}','{}')", (reversal, T0))
            circle = rules.Finding("circling", "patterns", "Circle", 3, "turns", "", "", 73, evidence={})
            circle.evidence["pattern"] = {"started_at": T0 + 90, "ended_at": T0 + 900}
            app.Detector(self.path)._absorb(db, "abc123", T0, circle, 99, T0 + 900)
            evidence = json.loads(
                db.execute("SELECT evidence FROM events WHERE id=?", (reversal,)).fetchone()[0]
            )
        self.assertEqual(evidence["absorbed_by"], {"kind": "circling", "event_id": 99})

    def test_a_reversal_saved_before_the_circle_verified_is_absorbed(self):
        run(
            app.Detector(self.path).process,
            {"abc123": stream(reversal_then_circle(), "abc123", LIGHT, wind=(5.0, 0.0))},
            tail=240,
        )
        self.assertEqual(self.events("course_reversal"), [])
        self.one("circling")


NOW = 1_791_150_000.0


def shape_row(db, kind, start, end, *, session=NOW, address="abc123"):
    """A saved pattern row of ``kind`` flown from ``start`` to ``end``."""
    aircraft = {"hex": address, "r": "N8568S", "t": "C172", "type": "adsb_icao"}
    if kind == "circling":
        finding = rules.Finding(kind, "patterns", "Circle", 3, "turns", "", "", 73, evidence={})
        finding.evidence = {"pattern": {"started_at": start, "ended_at": end}}
    else:
        finding = reversal_finding(start)
    return app.record(db, aircraft, finding, start, end, extra={"encounter_start": session})


def absorb(db, row, by, kind="circling"):
    db.execute(
        "UPDATE events SET evidence=json_set(evidence,'$.absorbed_by',json(?)) WHERE id=?",
        (json.dumps({"kind": kind, "event_id": by}), row),
    )


class ExportedAbsorptionTests(JournalCase):
    """Absorbed rows stay in their encounter's observation without moving its bounds."""

    QUIET = NOW + 30000

    def exported(self, now=QUIET):
        return [row["observation"]["group"] for row in export.snapshot(self.path, now)]

    def test_an_absorbed_reversal_leaves_the_encounter_bounds_to_the_circle(self):
        with app.connect(self.path) as db:
            reversal = shape_row(db, "course_reversal", NOW, NOW + 200)
            circle = shape_row(db, "circling", NOW + 600, NOW + 1200)
            absorb(db, reversal, circle)
        (group,) = self.exported()
        self.assertEqual((group["first_seen"], group["last_seen"]), (NOW + 600, NOW + 1200))
        self.assertEqual(sorted(event["kind"] for event in group["events"]), ["circling", "course_reversal"])
        absorbed = next(event for event in group["events"] if event["kind"] == "course_reversal")
        self.assertEqual(absorbed["evidence"]["absorbed_by"], {"superseded": True})
        # The bounds and start are the ones the legacy autopilot.collect reads.
        with app.connect(self.path) as db:
            (legacy,) = selection.encounters(db, self.QUIET, identity_cooldown=False)
        self.assertEqual(
            (group["first_seen"], group["last_seen"], group["start"]),
            (legacy["first_seen"], legacy["last_seen"], legacy["start"]),
        )

    def test_an_absorbed_row_whose_shape_is_gone_stays_with_its_encounter(self):
        with app.connect(self.path) as db:
            reversal = shape_row(db, "course_reversal", NOW, NOW + 200)
            shape_row(db, "circling", NOW + 600, NOW + 1200)
            absorb(db, reversal, 999999)
        (group,) = self.exported()
        self.assertEqual((group["first_seen"], group["last_seen"]), (NOW + 600, NOW + 1200))
        self.assertEqual(sorted(event["kind"] for event in group["events"]), ["circling", "course_reversal"])

    def test_an_absorbed_row_whose_shape_is_gone_keeps_its_own_group(self):
        with app.connect(self.path) as db:
            reversal = shape_row(db, "course_reversal", NOW, NOW + 200)
            absorb(db, reversal, 999999)
        (group,) = self.exported()
        self.assertEqual([event["kind"] for event in group["events"]], ["course_reversal"])
        self.assertEqual((group["first_seen"], group["last_seen"]), (NOW, NOW + 200))


if __name__ == "__main__":
    unittest.main()
