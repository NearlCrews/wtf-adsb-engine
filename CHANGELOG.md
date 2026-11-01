# Changelog

User-visible changes are recorded here. Application releases, plugin SDK versions,
and evidence contract versions are tracked separately.

## Unreleased

The package currently identifies itself as 0.1.0. This section describes the
initial development version and does not announce a published release.

### Added

- Private factual viewer and feedback, with a separate immutable saved-writing
  archive; installed static assets and preserved monitoring endpoints.
- Verified native journal migration, interrupted-transfer startup holds,
  independent process liveness and feed readiness checks.
- Self-contained Linux/amd64 container builds from locked source, including
  archive tools, an unprivileged account and build/package inventories. Offline
  image checks cover replay, viewer assets, restart and installed plugin images.
- Compose templates and opt-in private
  FlightAware and fleet-reference configuration.
- Receiver JSON collection, continuous collection, immutable NDJSON replay, and
  a read-only adapter for compatible analysis journals.
- Installed-package plugin interfaces for inputs, detectors, reference providers,
  enrichment, JSON formatting, and delivery destinations.
- Versioned collection evidence with stable IDs, revisions, explicit retractions,
  witnessed supersessions, and independent output cursors.
- Schema and privacy validation, reviewed extension schemas, and an external
  enrichment plugin example.
- Private archive freezing, pinned replay, metrics analysis, and compact replay
  importing into journal backups.
- MIT licensing, public documentation, contributor guidance, offline checks,
  source and wheel builds, and a GitHub Actions check workflow.

### Changed

- The analysis modules were resynced with the original application's analysis
  as of 31 October 2026 by three-way merge from the 9 October extraction, keeping
  every fix listed below. A kind registry (`analysis/kinds.py`) and the
  enrichment rules the facts layer reads (`analysis/enrichment_rules.py`) now
  define the kind tables. Curation holds a repeated kind on one airframe for 30
  days, a type speed mismatch names a disagreeing registry's type instead of
  being held by it, and selection derives per-type ground-speed norms.
- Evidence contract 1.1.0 adds an optional `context.type_norms` object: for
  each seasonal speed record or type speed mismatch, its type's usual ground
  speed in these records, or null. A finding's norm is frozen at its group's
  first quiet export, so the moving sample window issues no new revisions.
  Upgrading an existing 1.0.0 stream issues one revision for each group with a
  norm-kind finding still in the journal view. Producers
  write 1.1.0; readers accept 1.0.0 and 1.1.0 and refuse other versions by name.
  The `wtf-adsb.collection.v1` profile is unchanged.
- Python 3.14.9 (including the container base image), Ruff 0.17.2, and NumPy
  2.5.4.

### Fixed

- Container build tooling uses uv 0.12.24, verified against its stable upstream
  release on 9 October 2026. (Development checks then used Ruff 0.17.0; see
  Changed for the current pin.)
- Repeated daily novelty context shares memory within a projection. Runtime
  copying preserves that sharing while isolating each observation's enrichment.
- Reference loading enforces its size bound during the read. Invalid oversized
  numeric measurements are rejected without an arithmetic exception, and failed
  journal connection setup releases its database handle.
- Unchanged observations and retractions read only the stored revision fields
  they need. Documentation checks now include package and maintenance guides.
- Journal inputs hold incomplete or failed analysis, and collection projections
  recheck health in the same read transaction as the evidence. Completed archive
  imports can use recorded aircraft timestamps without live receiver metadata.
- Output workers retry transient database failures, including failures to save
  delivery status, without losing their cursor or persisted artifact bytes.
- Input plugins receive their pipeline mode and isolated reference results;
  missing or failed required references hold their pipeline.
- Format plugins keep their own state directory during initialization and
  encoding, separate from the delivery plugin.
- Detector-supplied extensions now pass the same schema approval and payload
  validation as enrichment results before the input checkpoint advances.
- Replay hashes and reads the same open file and rejects metadata changes
  during the read, preventing records from advancing under the wrong file hash.
- Manual retractions acquire the engine lock without loading or starting plugins.
- Unchanged identity snapshots avoid database row rewrites; failed identity-store
  initialization releases its SQLite connection and any acquired writer lock.

### Known limits

- Linux and Python 3.14.9 are the tested platform. Native Windows is unsupported.
- Stateful detector rules and provider clients share a compatibility plugin.
- Format plugins preserve the JSON envelope. Hard plugin isolation, automatic
  history pruning, generation recovery, and online privacy erasure are deferred.
- Three reference bundles with unresolved redistribution terms are excluded.
