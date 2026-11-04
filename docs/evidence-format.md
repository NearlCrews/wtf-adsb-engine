# Evidence format

Version 0.1.0 implements the `wtf-adsb.collection.v1` profile with contract version
`1.1.0`, and reads `1.0.0` batches and manifests as well. The authoritative structural contracts are the bundled
[Draft 2020-12 JSON Schemas](../contracts/README.md); the wire validator adds
semantic checks. The [example batch](../examples/change-batch.example.json) uses
synthetic evidence and a valid digest.

The engine exports evidence independently of a consumer's selection,
presentation, or publication policy. Consumers can bundle the released schemas
and validate the JSON without engine imports or access to engine journals.
Schema and example copies retain the project's MIT notice. A structural schema
check must be accompanied by the semantic checks described below.

## Files and stream continuity

The file output writes this layout; each sequence number has 20 digits:

```text
export/
  manifest.json
  batches/
    00000000000000000001-00000000000000000100.json
    00000000000000000101-00000000000000000200.json
```

The manifest contains the stream and generation UUIDs, exact profile and contract
version, current delivered head, typed health and the complete batch inventory.
Each entry gives a relative path, byte count, SHA-256 and inclusive sequence
interval. `first_sequence` is always 1 in this release. The inventory contains
every change through the delivered head, including retractions.

The sink flushes and atomically renames a batch before replacing its manifest.
Readers reject symlinks, absolute paths, traversal, excessive sizes, checksum
failures, sequence gaps and unsupported versions. Checksums detect corruption;
filesystem access controls establish who may supply the feed. Use a private,
read-only mount for the consumer.

Each output has its own persisted cursor and saved artifact bytes. A crash after
delivery but before acknowledgment can cause a duplicate, so delivery is at
least once. Retries reuse the assigned envelope and the exact bytes already
stored for that destination. Format plugins must preserve the complete JSON
envelope; they cannot silently filter a numbered stream.

## Batch and observation shapes

| Field | Implemented meaning |
| --- | --- |
| `schema`, `schema_version`, `profile` | `wtf-adsb.observations`, `1.1.0` (or `1.0.0` from an earlier producer), `wtf-adsb.collection.v1` |
| `stream_id`, `generation` | Persistent opaque UUIDs identifying the stream and its continuity |
| `generated_at` | Stable UTC timestamp assigned with the batch, taken from its last change |
| `producer` | Bounded software name and version tokens recorded with the changes |
| `sequence_start`, `sequence_end` | Inclusive contiguous change positions |
| `synthetic` | Explicit fixture marker; production consumers refuse synthetic batches |
| `changes` | Ordered `upsert` or `retract` operations |
| `extension_schemas` | Optional exact namespace, version and schema-digest declarations |

Every change has a sequence, operation, event UUID, monotonically increasing
revision and UTC update time. An upsert includes the complete current observation
and its evidence digest. A retraction carries a controlled reason and may name
superseding UUIDs; it does not repeat old evidence. The wire vocabulary includes
`privacy_removal`, but the current producer deliberately rejects that operation
because it cannot purge previously delivered history.

The collection observation has these fields:

| Field | Content |
| --- | --- |
| `group` | Stable group UUID, observation interval and an array of projected findings |
| `context` | Bounded novelty counts, the named compatibility policy profile, and, since 1.1.0, per-finding type norms |
| `media` | Aircraft track parts keyed by finding UUID, with timestamped positions and available altitude/speed references. The parts come from the first of `maneuver_samples`, `pattern_samples`, `track`, and `context_samples` that holds positions, so a TCAS finding is drawn from its context samples |
| `extensions` | Optional namespaced, versioned payloads approved by local schema configuration |

Findings retain their kind, interval, evidence, available aircraft identity,
validation verdicts, claim limits and compatibility selection fields. Structural
schemas define the accepted shape; the reviewed projection in `analysis/export.py`
selects the evidence fields and removes receiver data. Known quality flags and
unknown blocking conditions remain holds. A schema pass alone does not establish
that a measurement is true or authorize publication.

A group's interval and start come from its selectable findings. A shape that a
later circle or survey grid covers stays in that encounter's group, marked
superseded, without widening the interval; one whose encounter has no other
finding left keeps a group of its own.

`context.type_norms` (contract 1.1.0) carries, for each seasonal speed record and
type speed mismatch in the group, the type's usual ground speed in these records:
the tenth and ninetieth percentiles of per-address encounter medians in the
finding's flight phase and 10,000 ft altitude band, over the 30 days of direct
samples before the newest, excluding the finding's own address. It needs 20 other
addresses over 14 days; otherwise the entry is null. The object is keyed by
finding ID and is absent when the group has no such finding, so other
observations are unchanged from 1.0.0. A norm is a population figure from one
collection, never a limit or a claim about one aircraft. While a group is
active, or while a pending or failed second-source type check holds the finding,
its norm follows the sample window. The first export after the group turns quiet
(30 minutes without reports) with no such hold freezes each finding's norm in the
identity sidecar with the type, phase, band, and address it read; later exports reuse it
while those stay the same, so the moving window does not issue a new revision on
every pass. A replacement restored after source retention keeps the norms of its
saved evidence. Upgrading an existing 1.0.0 stream issues one revision for each
group with a norm-kind finding that is still in the journal view; a fresh stream
is unaffected.

This is a compatibility profile for the extracted analysis. Its named policy
context preserves the existing grouping and validation semantics without making
them universal requirements for other consumers. A normalized model with separate
measurement records, explicit units and rule-level provenance remains a later
profile. Consumers must not assume a normalized model is available.

## Evidence blocks for newer kinds

Six kinds joined the registry after 0.1.0: `winds_aloft_day`, `foreign_military`,
`first_operator_here`, `aerobatic_practice`, `unregistered_address`, and
`racetrack_orbit`. Their blocks sit inside a finding's evidence object, which the
contract leaves open, so contract 1.1.0 carries them unchanged. The export
allowlist in `analysis/export.py` drops any other field.

### `winds_aloft`

A `winds_aloft_day` finding describes a UTC day, not one aircraft. It counts the
direct ADS-B reports from level jets at or above FL280 flying roughly east or
west, at most one a minute for each address and direction, and fires when 30
addresses each way show eastbound median ground speeds at least 150 kn above
westbound ones. It is saved on the fastest eastbound address with a recent
report, later evaluations that day keep that address, and the kind is marked
`subjectless`. The block keeps the day's last passing evaluation, also when the
asymmetry later drops below the threshold. Its maneuver block ends with
`end_reason` `day_closed` after UTC midnight.

| Field | Content |
| --- | --- |
| `day` | The UTC day, `YYYY-MM-DD` |
| `east_addresses`, `west_addresses` | Addresses counted in each direction |
| `east_median_kn`, `west_median_kn` | Median of the per-address median ground speeds in each direction |
| `east_quartiles_kn`, `west_quartiles_kn` | The lower and upper quartiles of those speeds, as a two-item array |
| `asymmetry_kn` | Eastbound median minus westbound median, rounded |
| `altitude_floor_ft` | The altitude floor, 28,000 ft |
| `evaluated_at` | Unix time of the evaluation that wrote the block |
| `wind_250_hpa` | Optional cached Open-Meteo forecast for the hour nearest the evaluation: `speed_kn`, `direction_deg`, `source` (`open-meteo`), and `hour` (`YYYY-MM-DDTHH:00` UTC). The grid cell's coordinates are never included. |

The finding's participants are up to 12 other eastbound addresses, fastest first.
Each carries `measurements.median_gs_kn`, its median ground speed for the day.
The wind itself is never measured; a forecast value is a model figure for one
grid cell.

### `foreign_military`

A `foreign_military` finding names an address that the aircraft database flags
as military, or that lies in a military allocation range, when the allocation
block belongs to a State outside the United States group. A registration that
disagrees with the allocation, or an identity conflict, withholds it.

| Field | Content |
| --- | --- |
| `country` | The allocation State's code |
| `country_name` | The allocation State's name |
| `basis` | One or both of `dbFlags` and `military_allocation` |
| `registration_country` | The country the registration implies, or null |

### `unregistered`

An `unregistered_address` finding marks a direct ADS-B report with a usable
position whose address no consulted registry lists, whether the aircraft database
lacks the address or lists it without a registration (or only with a
placeholder). Addresses in the military allocation ranges and privacy (PIA)
addresses are excluded. It carries the `registry_gap` context tag. The kind is
type-dependent and is not identity-led, so it never waits on identity
corroboration. The type-check worker looks the address up again once its saved
check is 7 days old, within 30 days of the first sighting; a failed recheck keeps
the saved answer, and an unanswered check never expires into "unavailable".
Selection blocks the finding while a second source names a registration or a
type for the address (`registry_lists_address`), and until a settled check whose
`type_check.checked_at`, the time the registry answered, is at least 7 days after
the first sighting (`registry_unchecked`).

| Field | Content |
| --- | --- |
| `callsign` | The broadcast callsign, or null |
| `category` | The ADS-B emitter category, or null |
| `source` | Always `adsb_icao` |

### `first_sighting` with scope `operator`

A `first_operator_here` finding uses the existing `first_sighting` block with
`scope` set to `operator`. It fires when an airline-style callsign carries a
designator that the operator table names, no earlier covered day in the retained
records had it, and those records cover at least 30 days. Only days whose daily
rows record designators count: a journal that gained the column on upgrade counts
from its next UTC day (the `operator_days_since` meta key), a new journal from
its first day.

| Field | Content |
| --- | --- |
| `scope` | `operator` |
| `designator` | The three-letter ICAO designator from the callsign |
| `operator` | The operator name from the shared designator table |
| `coverage_days` | Earlier days with at least an hour of coverage that record designators |
| `since` | The first such day in the retained records, or null |

### Aerobatic and racetrack metric keys

These keys appear in the finding's `maneuver` block.

`aerobatic_practice` needs at least five altitude reversals of 800 ft or more in
the last ten minutes of a track, an excursion of at least 1,500 ft, and a span of
at most 5 NM.

| Key | Content |
| --- | --- |
| `reversals` | Altitude reversals of at least 800 ft |
| `altitude_excursion_ft` | Highest minus lowest altitude in the window |
| `span_nm` | Diagonal of the window's horizontal bounding box |
| `max_turn_rate_dps` | Highest position-derived turn rate held for 20 s, in deg/s; absent when no turn was held that long |
| `basis` | `type` for a listed aerobatic designator, `turn_rate` for a light aircraft that held a turn of at least 6 deg/s |

A finding tagged `terminal_area_context` or `runway_circuit_context` is context
only and never a story on its own.

`racetrack_orbit` needs at least four alternating legs, each at least 10 NM long
and within 10 degrees of a common axis, over at least 40 minutes, with the two
lines 2 to 15 NM apart.

| Key | Content |
| --- | --- |
| `racetrack_legs` | Legs in the published run |
| `leg_length_nm` | Mean leg length |
| `axis_heading_deg` | Heading of the racetrack axis |
| `lateral_spacing_nm` | Spacing between the two lines |
| `line_repeat_nm` | Largest offset of a leg from its line, at most 1 NM |
| `runway_within_nm` | Distance to the nearest mapped runway end, capped at 50 NM; absent when the distance is unknown (no runway table, or a racetrack center within 50 NM of the table's edge or outside it) |

A racetrack can carry the `tanker_type`, `military_flag`, and `airport_context`
tags. `airport_context` means the racetrack was centered within 10 NM of a mapped
runway.

### Other new evidence keys

Rapid climb and descent findings record the rule that confirmed them:
`threshold_fpm`, `qualifying_seconds_required`, and
`qualifying_reports_required`. A descent starting at or above 10,000 ft needs
6,000 ft/min for 60 s and 6 reports; other episodes need 4,500 ft/min for 30 s
and 4 reports. A descent holding 7,000 ft/min for 30 s and 4 reports is confirmed
as exceptional. The `season_record` block adds `margin_kn`, the amount by which
the speed beat the previous maximum. A go-around carries
`airport_maneuver_hypothesis` as a context tag.

### Participants

A multi-aircraft finding lists the other aircraft in `participants`, at most 12,
never including the primary aircraft. Each entry is built from this allowlist:

| Field | Content |
| --- | --- |
| `hex` | The participant's ICAO address |
| `role` | `threat`, `lead`, `wingman`, or `member` |
| `registration`, `callsign`, `type` | Identity from the participant's aircraft record, when known |
| `source` | The participant's position source |
| `measurements` | Numeric readings and separations, such as `min_separation_nm` and `vertical_separation_ft` |
| `quality_flags` | Optional review flags for the participant's reports |
| `track` | Optional. The participant's own positions, oldest first, at most 96: `ts`, `lat`, `lon`, `source`, and `alt_baro`, `alt_geom`, and `gs` when reported. A formation member's track holds its reports matched in the formation; a TCAS threat's track covers 30 s before the first advisory message to 30 s after the last. No receiver-relative field is kept. |

## Identity, persistence and bounds

The core commits accepted observations, revisions, change-log entries and the
input checkpoint in one SQLite transaction. Evidence changes produce a new
revision; unchanged evidence produces none. Failed validation or enrichment
leaves the previous input checkpoint intact for retry. Disappearance from a
journal snapshot never implies retraction.

Compatibility journals use a private identity sidecar. Random finding and group
UUIDs survive interval corrections through saved row mappings, membership and
encounter aliases. Journal row numbers and aliases do not enter the public
contract. Witnessed membership merges and splits allocate replacement group IDs
and emit explicit supersession retractions, which the core commits with their
replacement observations. The sidecar retains pending replacement evidence for
retries. Duplicate identities, changed row subjects and invalid saved
supersessions stop export advancement for reconciliation. The core separately
maps durable plugin keys to event UUIDs. Preserve the complete state directory
when backing up or moving an engine.

Batch assignment is durable and limited to 100 changes with a 4 MiB change-payload
budget. A batch also splits when producer identity changes or when extension
schema versions conflict. Wire validation caps an encoded batch at 8 MiB and an
observation at 1 MiB, with additional nesting, string and array bounds. Oversized
or invalid evidence fails processing; evidence is never silently truncated to
advance the cursor. Storage exhaustion remains a service failure even though
output workers otherwise run independently from collection.

Evidence and local extension-schema digests use Python JSON serialization with
sorted object keys, UTF-8, `ensure_ascii=False`, `allow_nan=False` and compact
separators. Array order is significant. This profile does not claim that arbitrary
JSON serializers spell numbers identically. A consumer must implement matching
digest rules and verify each file's exact bytes. Use the supplied synthetic
fixtures to check compatibility before consuming a live feed.

## Health and consumer behavior

Manifest health has exactly five fields: boolean `ready` and `complete`, `mode`
(`live` or `replay`), and `last_receiver_poll` plus `analysis_watermark` as finite
nonnegative Unix timestamps or null. A ready live feed requires both timestamps.
A pipeline failure clears readiness and completeness while preserving the last
successful stamps. The file sink also marks an incomplete delivery backlog.

Consumers must store accepted changes and their cursor transactionally, deduplicate
retries, and stop at the last verified state on conflicting revisions or broken
continuity. Do not treat a partly imported manifest as a complete feed. New live
decisions need a separate freshness and completeness check. Replay is historical
and should require explicit opt-in for actions that normally use live evidence.
An explicit retraction invalidates dependent output; an absent record does not.

An extension is `{namespace, schema_version, payload}`. The engine requires a
reviewed local schema and validates every payload before committing. Each batch
declares the canonical schema digest; changing a schema under an existing
namespace/version is rejected. Received records cannot name executable code or
remote schemas. Consumers must hold observations with unsupported extensions
until their schemas and meaning have been reviewed. Installation alone grants no
permission to publish their fields.

## Retention, privacy and deferred work

The current file transport retains every batch beginning at sequence 1. There is
no automatic pruning, recovery snapshot, checkpoint message or generation-reset
command. Inventory limits eventually stop delivery; operators must monitor state
size. A new stream, changed generation, missing history or rollback cannot be
silently accepted as continuation.

The export projection excludes receiver coordinates and markers, range/bearing,
private hostnames, local paths, raw journal identifiers and private prose. Allowed
aircraft tracks can still suggest the reception region. Extension schemas require
the same privacy review as built-in evidence; schema validation is not a sandbox
or proof that arbitrary text is safe to release.

Online privacy purge is deferred. If forbidden data reaches retained history,
stop distribution and publication, sanitize retained producer and consumer
artifacts, and coordinate a replacement stream with explicit identity and
withdrawal reconciliation. Reopening a fresh directory or emitting a tombstone
does not erase the old copies. This release does not automate that recovery.

Future work includes normalized evidence profiles, bounded retained-history
recovery, automated privacy erasure, and additional transports. A filtered stream
will need its own contiguous sequence space and explicit scope removals. These
capabilities require new contracts and cross-project recovery tests before use.
