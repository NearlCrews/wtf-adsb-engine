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
| `context` | Bounded novelty counts, the named compatibility policy profile and, since 1.1.0, per-finding type norms |
| `media` | Aircraft track parts keyed by finding UUID, with timestamped positions and available altitude/speed references |
| `extensions` | Optional namespaced, versioned payloads approved by local schema configuration |

Findings retain their kind, interval, evidence, available aircraft identity,
validation verdicts, claim limits and compatibility selection fields. Structural
schemas define the accepted shape; the reviewed projection in `analysis/export.py`
selects the evidence fields and removes receiver data. Known quality flags and
unknown blocking conditions remain holds. A schema pass alone does not establish
that a measurement is true or authorize publication.

`context.type_norms` (contract 1.1.0) carries, for each seasonal speed record and
type speed mismatch in the group, the type's usual ground speed in these records:
the tenth and ninetieth percentiles of per-address encounter medians in the
finding's flight phase and 10,000 ft altitude band, over the 30 days of direct
samples before the newest, excluding the finding's own address. It needs 20 other
addresses over 14 days; otherwise the entry is null. The object is keyed by
finding ID and is absent when the group has no such finding, so other
observations are unchanged from 1.0.0. A norm is a population figure from one
collection, never a limit or a claim about one aircraft. Once a group is quiet
(30 minutes without reports) its findings keep the norm they were last exported
with, so the moving sample window does not issue a new revision on every pass;
the identity sidecar remembers that value, and a replacement restored after
source retention keeps the norms of its saved evidence.

This is a compatibility profile for the extracted analysis. Its named policy
context preserves the existing grouping and validation semantics without making
them universal requirements for other consumers. A normalized model with separate
measurement records, explicit units and rule-level provenance remains a later
profile. Consumers must not assume a normalized model is available.

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
