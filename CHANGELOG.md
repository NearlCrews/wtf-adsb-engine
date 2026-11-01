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
