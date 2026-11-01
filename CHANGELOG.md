# Changelog

User-visible changes are recorded here. Application releases, plugin SDK versions,
and evidence contract versions are tracked separately.

## Unreleased

## 0.1.0 (2026-11-01)

The first public release. The package identifies itself as 0.1.0, plugins
target SDK API version 1, and producers write evidence contract 1.1.0. Readers
accept evidence contract 1.0.0 and 1.1.0 and refuse other versions by name.

### Added

- Receiver JSON collection, continuous collection, immutable NDJSON replay, and
  a read-only adapter for compatible analysis journals. The continuous receiver
  input judges availability by the age of its last committed snapshot, so a
  failed collection attempt leaves readiness intact while that snapshot is at
  most `max_age_seconds` old; a collector fault fails the next pass at once.
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
  `wtf-adsb.collection.v1` profile is unchanged from 1.0.0.
- Output workers that retry transient database failures without losing their
  cursor or the artifact bytes they have saved.
- Schema and privacy validation, reviewed extension schemas, and an external
  enrichment plugin example.
- Private factual viewer and feedback, with a separate immutable saved-writing
  archive; installed static assets and preserved monitoring endpoints.
- Verified native journal migration, interrupted-transfer startup holds,
  independent process liveness and feed readiness checks.
- Self-contained Linux/amd64 container builds from locked source on Python
  3.14.9, including archive tools, an unprivileged account, and build and
  package inventories. Tagged releases publish the image to the GitHub
  Container Registry and attach the inventories to the GitHub release.
- Compose templates, which give the engine and viewer a 512 MiB `/tmp` tmpfs
  for SQLite's temporary files, opt-in private FlightAware and fleet-reference
  configuration, and `tools/setup.py`, which writes the configuration from an
  interview's answers, checks it, and starts the services.
- Private archive freezing, pinned replay, metrics analysis, and compact replay
  importing into journal backups.
- MIT licensing, public documentation, contributor guidance, and source and
  wheel builds.

### Known limits

- Linux and Python 3.14.9 are the tested platform. Native Windows is unsupported.
- Stateful detector rules and provider clients share a compatibility plugin.
- Format plugins preserve the JSON envelope. Hard plugin isolation, automatic
  history pruning, generation recovery, and online privacy erasure are deferred.
- The aircraft-types, identity, and notable-variants reference bundles are
  omitted while their redistribution terms are unresolved. The engine warns
  that they are missing and continues with its built-in rules.
