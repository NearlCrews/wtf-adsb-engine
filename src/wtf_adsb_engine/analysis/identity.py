"""Private identity continuity for journals that predate engine UUIDs.

Legacy row numbers stay inside this sidecar. Public finding/group identities are
random UUIDs. Membership and saved encounter aliases preserve identities when a
correction changes an interval. Witnessed membership merges and splits issue
explicit durable supersessions; simple interval corrections keep their IDs.
The sidecar also keeps each norm-kind finding's type norm from its group's first
quiet export, which later exports reuse (export.py).
"""

import json
import sqlite3
from pathlib import Path
from uuid import uuid4

from . import selection, stories
from .numeric import number


class IdentityReconciliationRequired(ValueError):
    pass


def _canonical(value):
    return json.dumps(value, separators=(",", ":"), allow_nan=False, sort_keys=True)


class IdentityStore:
    def __init__(self, path, source_instance):
        self.path = Path(path)
        self.source_instance = source_instance
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(self.path, timeout=30)
        try:
            self.db.row_factory = sqlite3.Row
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS finding_slots(
                    source TEXT NOT NULL,local_id INTEGER NOT NULL,identity TEXT NOT NULL,
                    address TEXT NOT NULL,kind TEXT NOT NULL,origin TEXT NOT NULL,
                    first_seen REAL NOT NULL,last_seen REAL NOT NULL,active INTEGER NOT NULL,
                    PRIMARY KEY(source,local_id));
                CREATE TABLE IF NOT EXISTS membership(
                    finding_id TEXT PRIMARY KEY,group_id TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS aliases(
                    source TEXT NOT NULL,anchor TEXT NOT NULL,group_id TEXT NOT NULL,
                    PRIMARY KEY(source,anchor));
                CREATE TABLE IF NOT EXISTS supersessions(
                    source TEXT NOT NULL,group_id TEXT NOT NULL,replacements TEXT NOT NULL,
                    PRIMARY KEY(source,group_id));
                CREATE TABLE IF NOT EXISTS replacement_payloads(
                    source TEXT NOT NULL,group_id TEXT NOT NULL,observation TEXT NOT NULL,
                    PRIMARY KEY(source,group_id));
                CREATE TABLE IF NOT EXISTS type_norms(
                    source TEXT NOT NULL,finding_id TEXT NOT NULL,inputs TEXT NOT NULL,
                    norm TEXT NOT NULL,PRIMARY KEY(source,finding_id));
            """)
            self.db.commit()
            self.path.chmod(0o600)
        except BaseException:
            self.db.close()
            raise
        self.rows = set()
        self.norm_findings = set()

    def __enter__(self):
        try:
            self.db.execute("BEGIN IMMEDIATE")
            self.retired = {
                row[0]: json.loads(row[1])
                for row in self.db.execute(
                    "SELECT group_id,replacements FROM supersessions WHERE source=?", (self.source_instance,)
                )
            }
            self._retractions = None
        except BaseException:
            self.db.close()
            raise
        return self

    def __exit__(self, kind, error, traceback):
        try:
            if kind is None:
                # Retention retires a local row slot, never its public identity.
                # A later reuse of a retired row gets a new random identity.
                previous = self.db.execute(
                    "SELECT local_id FROM finding_slots WHERE source=? AND active=1", (self.source_instance,)
                ).fetchall()
                self.db.executemany(
                    "UPDATE finding_slots SET active=0 WHERE source=? AND local_id=?",
                    [(self.source_instance, row[0]) for row in previous if row[0] not in self.rows],
                )
                self.db.commit()
            else:
                self.db.rollback()
        finally:
            self.db.close()

    def finding(self, source):
        local_id = source["id"]
        if not isinstance(local_id, int) or isinstance(local_id, bool):
            raise ValueError("Legacy journal requires integer local row identities")
        address, kind = str(source["hex"]).lower(), source["kind"]
        origin = stories.provenance(source, source["evidence"])
        first, last = number(source["first_seen"]), number(source["last_seen"])
        if first is None or last is None or first > last:
            raise ValueError("Invalid legacy finding interval")
        previous = self.db.execute(
            "SELECT * FROM finding_slots WHERE source=? AND local_id=?", (self.source_instance, local_id)
        ).fetchone()
        identity = None
        if previous:
            same_subject = (address, kind, origin) == (
                previous["address"],
                previous["kind"],
                previous["origin"],
            )
            overlaps = (
                first <= previous["last_seen"] + selection.ENCOUNTER_GAP
                and previous["first_seen"] <= last + selection.ENCOUNTER_GAP
            )
            if previous["active"] and same_subject and overlaps:
                identity = previous["identity"]
            elif previous["active"]:
                raise IdentityReconciliationRequired(
                    "A legacy row changed its subject or moved to an unrelated interval"
                )
        identity = identity or str(uuid4())
        if (
            previous is None
            or previous["identity"] != identity
            or previous["first_seen"] != first
            or previous["last_seen"] != last
            or not previous["active"]
        ):
            self.db.execute(
                "INSERT OR REPLACE INTO finding_slots VALUES (?,?,?,?,?,?,?,?,1)",
                (self.source_instance, local_id, identity, address, kind, origin, first, last),
            )
        self.rows.add(local_id)
        return identity

    def type_norm(self, finding_id, inputs, quiet, compute):
        """A finding's type norm, frozen at its group's first quiet export.

        While the group is active, compute() and save nothing. Once it is quiet, reuse the
        saved norm while it reads the same type, phase, band, and address; otherwise
        compute(), save it with those inputs, and reuse it from then on. A drifting sample
        window then changes no quiet group's revision.
        """
        self.norm_findings.add(finding_id)
        if not quiet:
            return compute()
        key = _canonical(inputs)
        row = self.db.execute(
            "SELECT inputs,norm FROM type_norms WHERE source=? AND finding_id=?",
            (self.source_instance, finding_id),
        ).fetchone()
        if row is not None and row["inputs"] == key:
            return json.loads(row["norm"])
        norm = compute()
        self.db.execute(
            "INSERT OR REPLACE INTO type_norms VALUES (?,?,?,?)",
            (self.source_instance, finding_id, key, _canonical(norm)),
        )
        return norm

    def retain_type_norms(self):
        """After a complete view, forget the saved norms of findings it no longer holds."""
        saved = self.db.execute("SELECT finding_id FROM type_norms WHERE source=?", (self.source_instance,))
        self.db.executemany(
            "DELETE FROM type_norms WHERE source=? AND finding_id=?",
            [(self.source_instance, row[0]) for row in saved.fetchall() if row[0] not in self.norm_findings],
        )

    def _previous_groups(self, anchor, findings):
        identities = set()
        for finding in findings:
            row = self.db.execute("SELECT group_id FROM membership WHERE finding_id=?", (finding,)).fetchone()
            if row:
                identities.add(row[0])
        row = self.db.execute(
            "SELECT group_id FROM aliases WHERE source=? AND anchor=?", (self.source_instance, anchor)
        ).fetchone()
        if row:
            identities.add(row[0])
        return identities - self.retired.keys()

    def assign_groups(self, groups):
        """Reconcile a complete view using witnessed finding membership.

        A connected old/new component keeps its identity only for a one-to-one
        transition. A merge, split or rearrangement creates explicit replacement
        groups. An old group with no current member or encounter alias is absent,
        not withdrawn: source retention alone cannot retire its observation.
        """
        self._retractions = None
        previous = [self._previous_groups(anchor, findings) for anchor, findings in groups]
        parents = list(range(len(groups)))

        def root(index):
            while parents[index] != index:
                parents[index] = parents[parents[index]]
                index = parents[index]
            return index

        by_previous, findings_seen, anchors = {}, set(), set()
        for index, ((anchor, findings), identities) in enumerate(zip(groups, previous, strict=True)):
            if anchor in anchors or findings_seen.intersection(findings):
                raise IdentityReconciliationRequired("Current groups have duplicate identities")
            anchors.add(anchor)
            findings_seen.update(findings)
            for identity in identities:
                if identity in by_previous:
                    parents[root(index)] = root(by_previous[identity])
                else:
                    by_previous[identity] = index
        components = {}
        for index in range(len(groups)):
            components.setdefault(root(index), []).append(index)
        assigned = [None] * len(groups)
        for indices in components.values():
            old = set().union(*(previous[index] for index in indices))
            if len(old) == 1 and len(indices) == 1:
                assigned[indices[0]] = next(iter(old))
            else:
                for index in indices:
                    assigned[index] = str(uuid4())
                if old:
                    replacements = sorted(assigned[index] for index in indices)
                    for identity in old:
                        self.retired[identity] = replacements
                        self.db.execute(
                            "INSERT INTO supersessions VALUES (?,?,?)",
                            (self.source_instance, identity, json.dumps(replacements)),
                        )
        for (anchor, findings), identity in zip(groups, assigned, strict=True):
            self.db.execute(
                """INSERT INTO aliases VALUES (?,?,?)
                   ON CONFLICT(source,anchor) DO UPDATE SET group_id=excluded.group_id
                   WHERE aliases.group_id<>excluded.group_id""",
                (self.source_instance, anchor, identity),
            )
            self.db.executemany(
                """INSERT INTO membership VALUES (?,?)
                   ON CONFLICT(finding_id) DO UPDATE SET group_id=excluded.group_id
                   WHERE membership.group_id<>excluded.group_id""",
                [(finding, identity) for finding in findings],
            )
        return assigned

    def retractions(self):
        """Idempotent retirement records, flattened to terminal replacement keys."""
        if self._retractions is not None:
            return self._retractions
        terminal = {}
        for identity in self.retired:
            pending, visiting = [(identity, False)], set()
            while pending:
                current, children_done = pending.pop()
                if current in terminal:
                    continue
                if current not in self.retired:
                    terminal[current] = {current}
                elif children_done:
                    terminal[current] = set().union(*(terminal[child] for child in self.retired[current]))
                    visiting.remove(current)
                    if not 1 <= len(terminal[current]) <= 100:
                        raise IdentityReconciliationRequired("Supersession exceeds the replacement limit")
                else:
                    if current in visiting:
                        raise IdentityReconciliationRequired("Invalid cycle in saved supersessions")
                    visiting.add(current)
                    pending.append((current, True))
                    pending.extend((child, False) for child in self.retired[current])
        self._retractions = [
            {"key": identity, "reason_code": "superseded", "replacement_keys": sorted(terminal[identity])}
            for identity in sorted(self.retired)
        ]
        return self._retractions

    def replacement_rows(self, rows):
        """Restore a pending replacement after source retention and core failure.

        The sidecar and core outbox cannot share a transaction. Keep only terminal
        replacement payloads so a committed supersession never points to evidence
        that disappeared before the core could receive its first revision.
        """
        present = {row["key"] for row in rows}
        required = {key for item in self.retractions() for key in item["replacement_keys"]}
        restored = []
        for key in sorted(required - present):
            row = self.db.execute(
                "SELECT observation FROM replacement_payloads WHERE source=? AND group_id=?",
                (self.source_instance, key),
            ).fetchone()
            if row is None:
                raise IdentityReconciliationRequired("Replacement evidence needs explicit recovery")
            restored.append({"key": key, "observation": json.loads(row[0])})
        return restored

    def remember_replacements(self, rows):
        required = {key for item in self.retractions() for key in item["replacement_keys"]}
        for row in rows:
            if row["key"] in required:
                self.db.execute(
                    """INSERT INTO replacement_payloads VALUES (?,?,?)
                       ON CONFLICT(source,group_id) DO UPDATE SET observation=excluded.observation
                       WHERE replacement_payloads.observation<>excluded.observation""",
                    (
                        self.source_instance,
                        row["key"],
                        json.dumps(
                            row["observation"], separators=(",", ":"), allow_nan=False, sort_keys=True
                        ),
                    ),
                )
        self.db.executemany(
            "DELETE FROM replacement_payloads WHERE source=? AND group_id=?",
            [(self.source_instance, identity) for identity in self.retired],
        )
