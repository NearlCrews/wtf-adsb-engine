"""Core-owned identity, input checkpoints, revisions, and durable change log."""

import json
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path

from . import __version__, wire


class Store:
    def __init__(self, path, *, synthetic=False):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.synthetic = synthetic
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS inputs(name TEXT PRIMARY KEY,checkpoint TEXT,health TEXT);
                CREATE TABLE IF NOT EXISTS identities(source_key TEXT PRIMARY KEY,event_id TEXT UNIQUE NOT NULL);
                CREATE TABLE IF NOT EXISTS records(event_id TEXT PRIMARY KEY,revision INTEGER NOT NULL,
                    digest TEXT NOT NULL,observation TEXT,active INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS changes(sequence INTEGER PRIMARY KEY AUTOINCREMENT,change TEXT NOT NULL,
                    producer TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS schemas(namespace TEXT NOT NULL,version TEXT NOT NULL,sha256 TEXT NOT NULL,
                    PRIMARY KEY(namespace,version));
                CREATE TABLE IF NOT EXISTS batches(sequence_start INTEGER PRIMARY KEY,sequence_end INTEGER NOT NULL,
                    envelope TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS artifacts(destination TEXT NOT NULL,sequence_start INTEGER NOT NULL,
                    artifact BLOB NOT NULL,PRIMARY KEY(destination,sequence_start));
                CREATE TABLE IF NOT EXISTS bindings(name TEXT PRIMARY KEY,fingerprint TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS deliveries(name TEXT PRIMARY KEY,cursor INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'waiting',error TEXT);
            """)
            for key, value in (
                ("stream_id", str(uuid.uuid4())),
                ("generation", str(uuid.uuid4())),
                ("synthetic", synthetic),
            ):
                db.execute("INSERT OR IGNORE INTO meta VALUES (?,?)", (key, json.dumps(value)))
            if (
                json.loads(db.execute("SELECT value FROM meta WHERE key='synthetic'").fetchone()[0])
                != synthetic
            ):
                raise ValueError("Cannot mix synthetic and real input in one stream")
        self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        if getattr(self, "read_only", False):
            db = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=30)
        else:
            db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @classmethod
    def inspect(cls, path):
        instance = cls.__new__(cls)
        instance.path = Path(path)
        instance.read_only = True
        return instance.state()

    def checkpoint(self, name):
        with self.connect() as db:
            row = db.execute("SELECT checkpoint FROM inputs WHERE name=?", (name,)).fetchone()
            return json.loads(row[0]) if row and row[0] else None

    def register_schemas(self, declarations):
        with self.connect() as db:
            for (namespace, version), checksum in declarations.items():
                previous = db.execute(
                    "SELECT sha256 FROM schemas WHERE namespace=? AND version=?", (namespace, version)
                ).fetchone()
                if previous and previous[0] != checksum:
                    raise ValueError("An immutable extension schema changed; increment its version")
                db.execute("INSERT OR IGNORE INTO schemas VALUES (?,?,?)", (namespace, version, checksum))

    def bind_source(self, source):
        with self.connect() as db:
            row = db.execute("SELECT value FROM meta WHERE key='source_instance'").fetchone()
            if row and json.loads(row[0]) != source:
                raise ValueError("State directory belongs to another source instance")
            db.execute("INSERT OR IGNORE INTO meta VALUES ('source_instance',?)", (json.dumps(source),))

    def bind_configuration(self, bindings):
        with self.connect() as db:
            for name, value in bindings.items():
                fingerprint = wire.digest(value)
                prior = db.execute("SELECT fingerprint FROM bindings WHERE name=?", (name,)).fetchone()
                if prior and prior[0] != fingerprint:
                    raise ValueError(
                        "A durable plugin instance changed identity or destination; configure a new instance name"
                    )
                db.execute("INSERT OR IGNORE INTO bindings VALUES (?,?)", (name, fingerprint))

    def active_inputs(self, names):
        with self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO meta VALUES ('active_inputs',?)", (wire.canonical(names).decode(),)
            )

    def commit(self, name, checkpoint, observations, health, *, timestamp, retractions=()):
        stamp = wire.utc(timestamp)
        prepared = []
        for observation in observations:
            wire.validate_observation(observation.data)
            if not isinstance(observation.key, str) or not observation.key or len(observation.key) > 1000:
                raise ValueError("Observation requires a stable private source key")
            prepared.append((observation.key, observation.data, wire.digest(observation.data)))
        prepared_keys = {row[0] for row in prepared}
        if len(prepared_keys) != len(prepared):
            raise ValueError("Duplicate observation identity in one transaction")
        wire.validate_health(health)
        changed = 0
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for key, payload, digest in prepared:
                found = db.execute("SELECT event_id FROM identities WHERE source_key=?", (key,)).fetchone()
                event_id = found[0] if found else str(uuid.uuid4())
                if not found:
                    db.execute("INSERT INTO identities VALUES (?,?)", (key, event_id))
                previous = db.execute(
                    "SELECT digest,revision FROM records WHERE event_id=?", (event_id,)
                ).fetchone()
                if previous and previous["digest"] == digest:
                    continue
                revision = previous["revision"] + 1 if previous else 1
                change = {
                    "operation": "upsert",
                    "event_id": event_id,
                    "revision": revision,
                    "updated_at": stamp,
                    "evidence_digest": digest,
                    "observation": payload,
                }
                self._append(db, change)
                db.execute(
                    "INSERT OR REPLACE INTO records VALUES (?,?,?,?,1)",
                    (event_id, revision, digest, wire.canonical(payload).decode()),
                )
                changed += 1
            retirements = {item.key: item for item in retractions}
            if len(retirements) != len(retractions):
                raise ValueError("Duplicate retirement identity")

            def replacements(key, visited):
                if key in visited:
                    raise ValueError("Cyclic supersession")
                if key in retirements:
                    return {
                        result
                        for child in retirements[key].replacement_keys
                        for result in replacements(child, visited | {key})
                    }
                found = db.execute("SELECT event_id FROM identities WHERE source_key=?", (key,)).fetchone()
                if not found:
                    raise ValueError("Supersession replacement has not been issued")
                return {found[0]}

            for key, retirement in retirements.items():
                if retirement.reason_code not in {"invalid_evidence", "superseded", "out_of_scope"}:
                    raise ValueError("Unsupported detector retraction")
                if key in prepared_keys:
                    raise ValueError("Cannot upsert and retract one identity in the same transaction")
                row = db.execute(
                    "SELECT r.event_id,r.revision,r.active FROM records r "
                    "JOIN identities i USING(event_id) WHERE i.source_key=?",
                    (key,),
                ).fetchone()
                if row is None or not row["active"]:
                    continue
                successors = sorted(
                    {event for child in retirement.replacement_keys for event in replacements(child, {key})}
                )
                if retirement.reason_code == "superseded" and not successors:
                    raise ValueError("Supersession requires replacement observations")
                if len(successors) > 100:
                    raise ValueError("Supersession exceeds the replacement limit")
                revision = row["revision"] + 1
                self._append(
                    db,
                    {
                        "operation": "retract",
                        "event_id": row["event_id"],
                        "revision": revision,
                        "updated_at": stamp,
                        "reason_code": retirement.reason_code,
                        **({"superseded_by": successors} if successors else {}),
                    },
                )
                db.execute(
                    "UPDATE records SET revision=?,active=0 WHERE event_id=?", (revision, row["event_id"])
                )
                changed += 1
            db.execute(
                "INSERT OR REPLACE INTO inputs VALUES (?,?,?)",
                (name, wire.canonical(checkpoint).decode(), wire.canonical(health).decode()),
            )
        return changed

    @staticmethod
    def _append(db, change):
        producer = wire.canonical({"name": "wtf-adsb-engine", "version": __version__}).decode()
        row = db.execute("INSERT INTO changes(change,producer) VALUES ('{}',?)", (producer,))
        change = {**change, "sequence": row.lastrowid}
        db.execute(
            "UPDATE changes SET change=? WHERE sequence=?", (wire.canonical(change).decode(), row.lastrowid)
        )

    def retract(self, event_id, reason, *, timestamp):
        if reason not in {"invalid_evidence", "privacy_removal", "superseded", "out_of_scope"}:
            raise ValueError("Unsupported retraction reason")
        if reason == "privacy_removal":
            raise ValueError(
                "Privacy purges require replacing retained exports and explicit consumer recovery; unsupported online"
            )
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT revision,active FROM records WHERE event_id=?", (event_id,)).fetchone()
            if not row:
                raise ValueError("Unknown event")
            if not row["active"]:
                return
            revision = row["revision"] + 1
            self._append(
                db,
                {
                    "operation": "retract",
                    "event_id": event_id,
                    "revision": revision,
                    "updated_at": wire.utc(timestamp),
                    "reason_code": reason,
                },
            )
            db.execute("UPDATE records SET revision=?,active=0 WHERE event_id=?", (revision, event_id))

    def cursor(self, name):
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO deliveries(name) VALUES (?)", (name,))
            return db.execute("SELECT cursor FROM deliveries WHERE name=?", (name,)).fetchone()[0]

    def acknowledged(self, name, cursor):
        with self.connect() as db:
            db.execute(
                "UPDATE deliveries SET cursor=MAX(cursor,?),status='ready',error=NULL WHERE name=?",
                (cursor, name),
            )

    def failed(self, name, error):
        with self.connect() as db:
            db.execute(
                "UPDATE deliveries SET status='failed',error=? WHERE name=?", (type(error).__name__, name)
            )

    def input_failed(self, name, timestamp, mode="live"):
        with self.connect() as db:
            row = db.execute("SELECT checkpoint,health FROM inputs WHERE name=?", (name,)).fetchone()
            health = (
                json.loads(row["health"])
                if row
                else {
                    "mode": mode,
                    "last_receiver_poll": None,
                    "analysis_watermark": None,
                }
            )
            health.update(ready=False, complete=False)
            db.execute(
                "INSERT OR REPLACE INTO inputs VALUES (?,?,?)",
                (
                    name,
                    row[0] if row else "null",
                    wire.canonical(health).decode(),
                ),
            )

    def state(self):
        with self.connect() as db:
            db.execute("BEGIN")
            meta = {r["key"]: json.loads(r["value"]) for r in db.execute("SELECT * FROM meta")}
            head = db.execute("SELECT COALESCE(MAX(sequence),0) FROM changes").fetchone()[0]
            saved = {r["name"]: json.loads(r["health"]) for r in db.execute("SELECT name,health FROM inputs")}
            inputs = [saved.get(name, {}) for name in meta.get("active_inputs", sorted(saved))]
            live = [h for h in inputs if h.get("mode") == "live"]
            health = {
                "ready": bool(inputs) and all(h.get("ready") for h in inputs),
                "complete": bool(inputs) and all(h.get("complete") for h in inputs),
                "mode": "live" if live else "replay",
                "last_receiver_poll": min((h.get("last_receiver_poll") or 0 for h in live), default=None),
                "analysis_watermark": min(
                    (h.get("analysis_watermark") or 0 for h in (live or inputs)), default=None
                ),
            }
            return {
                **meta,
                "head_sequence": head,
                "health": health,
                "outputs": [dict(r) for r in db.execute("SELECT * FROM deliveries ORDER BY name")],
            }

    def next_batch(self, cursor):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            assigned = db.execute(
                "SELECT * FROM batches WHERE sequence_end>? ORDER BY sequence_start LIMIT 1", (cursor,)
            ).fetchone()
            if assigned:
                if assigned["sequence_start"] != cursor + 1:
                    raise ValueError("Output checkpoint is inside an assigned batch")
                return json.loads(assigned["envelope"])
            rows = db.execute(
                "SELECT change,producer FROM changes WHERE sequence>? ORDER BY sequence LIMIT 100", (cursor,)
            ).fetchall()
            if not rows:
                return None
            state = {r["key"]: json.loads(r["value"]) for r in db.execute("SELECT * FROM meta")}
            changes, declarations, versions = [], {}, {}
            size = 0
            for row in rows:
                change = json.loads(row["change"])
                extensions = change.get("observation", {}).get("extensions", [])
                if changes and (
                    row["producer"] != rows[0]["producer"]
                    or size + len(row["change"].encode()) > 4 * 1024 * 1024
                    or any(
                        versions.get(e["namespace"], e["schema_version"]) != e["schema_version"]
                        for e in extensions
                    )
                ):
                    break
                for extension in extensions:
                    namespace, version = extension["namespace"], extension["schema_version"]
                    schema = db.execute(
                        "SELECT sha256 FROM schemas WHERE namespace=? AND version=?", (namespace, version)
                    ).fetchone()
                    if schema is None:
                        raise ValueError("Outbox extension schema is unavailable")
                    versions[namespace] = version
                    declarations[namespace] = {
                        "namespace": namespace,
                        "schema_version": version,
                        "sha256": schema[0],
                    }
                size += len(row["change"].encode())
                changes.append(change)
            batch = wire.validate_batch(
                {
                    "schema": "wtf-adsb.observations",
                    "schema_version": wire.VERSION,
                    "profile": wire.PROFILE,
                    "stream_id": state["stream_id"],
                    "generation": state["generation"],
                    "generated_at": changes[-1]["updated_at"],
                    "producer": json.loads(rows[0]["producer"]),
                    "sequence_start": changes[0]["sequence"],
                    "sequence_end": changes[-1]["sequence"],
                    "synthetic": state["synthetic"],
                    "changes": changes,
                    **({"extension_schemas": list(declarations.values())} if declarations else {}),
                },
                allow_synthetic=state["synthetic"],
            )
            db.execute(
                "INSERT INTO batches VALUES (?,?,?)",
                (batch["sequence_start"], batch["sequence_end"], wire.canonical(batch).decode()),
            )
            return batch

    def artifact(self, destination, sequence):
        with self.connect() as db:
            row = db.execute(
                "SELECT artifact FROM artifacts WHERE destination=? AND sequence_start=?",
                (destination, sequence),
            ).fetchone()
            return row[0] if row else None

    def save_artifact(self, destination, sequence, artifact):
        with self.connect() as db:
            db.execute("INSERT INTO artifacts VALUES (?,?,?)", (destination, sequence, artifact))
