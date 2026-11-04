# Changelog

User-visible changes are recorded here. Application releases, plugin SDK versions,
and evidence contract versions are tracked separately.

## Unreleased

### Added

- Six kinds: `winds_aloft_day`, `foreign_military`, `first_operator_here`,
  `aerobatic_practice`, `unregistered_address`, and `racetrack_orbit`, with
  their evidence blocks and export allowlist entries. The evidence format
  document lists the new blocks and metric keys.
- A `subjectless` kind property for stories with no aircraft subject. The
  winds-aloft day is the first such kind.
- The operator designator table `airline_designators.json`, shared with the
  publisher byte for byte, and `reference.operator_name()`, which returns the
  operator name for a listed three-letter ICAO designator.
  `reference.operator_designator()` is unchanged.
- A `designator` column in the `daily` table, added to existing journals on
  start. Operator days and their coverage count from the `operator_days_since`
  meta key: the next UTC day for an upgraded journal, the first day for a new
  one. The history backfill's cross-day phase is now `cross-day-v3` and counts
  operator days.
- A `day_speed_samples` journal table that keeps each address's directional
  ground speeds for a UTC day, at most one a minute, held for 3 days, and the
  `end_reason` value `day_closed`.
- The context tags `registry_gap`, `tanker_type`, `military_flag`, and
  `airport_context`, with their notes.
- Rapid climb and descent findings record `threshold_fpm`,
  `qualifying_seconds_required`, and `qualifying_reports_required`, and
  seasonal speed records record `margin_kn`.
- Participants of formation and TCAS findings carry their own track.

### Fixed

- TCAS findings export drawable media parts. The parts now come from the first
  sample list that holds positions, so the context samples supply them when the
  maneuver samples are positionless advisory reports.
- Continuous collection no longer fails a poll with "database is locked" when
  enrichment writes the journal at the same moment. The collector now takes the
  journal's write lock before it reads, so it waits for another writer instead
  of failing at once.

### Changed

- A failed projection pass or output delivery now logs a warning with the
  exception class and a short message, so operators can tell a stale receiver
  from an unavailable one or a schema failure. URLs, absolute paths, keys,
  tokens, and host names are removed, and the message is cut to 200 characters.
  The JSON status line still names only the exception class.
- A rapid descent that starts at or above 10,000 ft needs 6,000 ft/min or
  faster for 60 s and 6 reports. Below 10,000 ft, and for climbs, the
  4,500 ft/min rule with 30 s and 4 reports stays. A descent that holds
  7,000 ft/min for 30 s and 4 reports is still confirmed as exceptional.
- A seasonal speed record must beat the previous maximum by 10 kn or 3 percent,
  whichever is larger, over a baseline of at least 30 days (was 14).
- A first-of-type, watch-type, vintage, notable-variant, or foreign-military
  finding is held with `reading_implausible` when the aircraft's own direct
  ADS-B report gives a ground speed the listed type cannot fly, or any
  pressure altitude is more than 2,000 ft above the type's ceiling. An MLAT or
  TIS-B speed does not trigger it.
- A call sign that differs from the registration only by formatting or one
  trailing letter no longer counts as an identity conflict. Neither does a
  registration that differs from the address's own N-number only by
  formatting. One trailing letter there is still a conflict, since N8447 and
  N8447L are different airframes.
- An address in a military allocation range corroborates the identity on its
  own.
- `identity_conflict` has a fixed score of 70 (was 78), and `holding_stack`
  has a fixed score of 78.
- Live spotting readings below the applied threshold are no longer journaled.
  The hourly prune deletes such live rows, and live rated rows the wind worker
  settled as within the limit, once last seen more than 7 days ago. Archive and
  replay rows are kept.
- A second-source type check still pending or failed 30 days after the first
  sighting counts as unavailable, so it no longer holds selection; an
  `unregistered_address` check never does. The export dates this by its own
  export time. `typecheck.pending` asks about the highest scores first, then
  the newest first sightings.
- `type_check.checked_at` is the time the registry answered, also when the
  worker attaches a retained check.
- An open `unregistered_address` finding has its registry lookup repeated once
  its saved check is a week old, within 30 days of the first sighting. A failed
  recheck keeps the saved answer. Selection blocks the finding while a second
  source names a registration or a type for the address
  (`registry_lists_address`), and until a settled check made at least a week
  after the first sighting (`registry_unchecked`). Privacy (PIA) addresses
  never make the finding.
- A possible go-around carries `airport_maneuver_hypothesis` as a context tag
  instead of a blocking quality flag.

## 0.1.0 (2026-11-01)

The first public release. The package identifies itself as 0.1.0, plugins
target SDK API version 1, and producers write evidence contract 1.1.0. Readers
accept evidence contract 1.0.0 and 1.1.0 and refuse other versions by name.

### Added

- Receiver JSON collection, continuous collection, immutable NDJSON replay, and
  a read-only adapter for compatible analysis journals, with readiness based on
  the age of the last committed snapshot.
- Installed-package plugin interfaces (SDK API version 1) for inputs, detectors,
  reference providers, enrichment, JSON formatting, and delivery destinations.
  Detector-supplied extensions pass the same schema approval and payload
  validation as enrichment results.
- Versioned collection evidence (contract 1.1.0) with stable IDs, revisions,
  explicit retractions, witnessed supersessions, and independent output
  cursors. Contract 1.1.0 adds an optional `context.type_norms` object: for
  each seasonal speed record or type speed mismatch, its type's usual ground
  speed in these records, or null. A finding's norm is frozen once its group is
  quiet, so the moving sample window issues no new revisions. The
  `wtf-adsb.collection.v1` profile is unchanged from 1.0.0. For integrators:
  delivery is at least once, so deduplicate on each batch's sequence range, and
  mount the feed read-only.
- Output workers that retry transient database failures without losing their
  cursor or the artifact bytes they have saved.
- Schema and privacy validation, reviewed extension schemas, and an external
  enrichment plugin example.
- A private factual viewer on port 8080 that accepts operator feedback and
  has health and metrics endpoints.
- An optional read-only archive view of AI-written notes kept by an earlier
  collector.
- A migration command that copies a compatible journal and refuses to start
  on an interrupted copy.
- Separate process liveness and feed readiness checks.
- Self-contained Linux/amd64 container builds from locked source on Python
  3.14.9, including archive tools, an unprivileged account, and build and
  package inventories. Tagged releases publish the image to the GitHub
  Container Registry and attach the inventories to the GitHub release.
- Compose templates that give the engine and viewer a 512 MiB `/tmp` tmpfs for
  SQLite's temporary files.
- Opt-in private FlightAware and fleet-reference configuration.
- `tools/setup.py`, which writes the configuration from an interview's
  answers, checks it, and starts the services. Under `sudo` it keeps the
  configuration with the operator, so the container account cannot change
  what root reads.
- Private archive freezing, pinned replay, metrics analysis, and compact replay
  importing into journal backups.
- Source and wheel builds under the MIT license.

### Known limits

- Linux and Python 3.14.9 are the tested platform. Native Windows is unsupported.
- Stateful detector rules and provider clients share a compatibility plugin.
- Format plugins preserve the JSON envelope. Hard plugin isolation, automatic
  history pruning, generation recovery, and online privacy erasure are deferred.
- The aircraft-types, identity, and notable-variants reference bundles are
  omitted while their redistribution terms are unresolved. The engine warns
  that they are missing and continues with its built-in rules.
