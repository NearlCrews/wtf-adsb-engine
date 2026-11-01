# Evidence contracts, version 1.1.0

These [Draft 2020-12 JSON Schemas](https://json-schema.org/draft/2020-12) describe
the implemented `wtf-adsb.collection.v1` profile. Consumers select an explicit
supported profile and version. The [evidence format](../docs/evidence-format.md)
defines delivery, identity, digest, and compatibility rules beyond JSON Schema.

| File | Validates |
| --- | --- |
| `observation-v1.schema.json` | A projected collection group, its findings, public context, aircraft media and extension envelopes |
| `change-batch-v1.schema.json` | Producer identity, stream/generation identity, ordered upserts or retractions, and extension declarations |
| `manifest-v1.schema.json` | The complete immutable batch inventory and typed stream health |

Each schema is self-contained and uses local JSON Pointer references. Copies in
the installed package and a consumer's pinned schema set must match these release assets byte
for byte. Validation reads bundled files without fetching schema URLs. The
`$schema` declaration identifies the standard dialect; it is not an instruction
to fetch a schema named by a received record.

Contract 1.1.0 adds the optional `context.type_norms` object. Producers write
1.1.0; the validator and these schemas also accept 1.0.0 batches and manifests,
so a stream that began under 1.0.0 stays readable. A 1.0.0 batch cannot carry
type norms, and any other version is refused as an unsupported contract before
the structural check. The profile stays `wtf-adsb.collection.v1`.

`type_norms` maps the ID of each norm-kind finding in the group (a seasonal speed
record or a type speed mismatch) to its type's usual ground speed in these
records, or to null when the records hold too few addresses or days. A norm has
exactly `low` and `high` (whole knots, rounded to five), `phase` (`climb`,
`descent`, or `level`), `band_floor_ft` and `band_ceiling_ft`, `addresses`,
`days`, and `window_days`. The wire validator requires every key to name a
finding of the same group; it does not know finding kinds, so limiting the keys
to norm-kind findings is the producer's rule. A group without a norm-kind
finding omits the object.

The wire validator applies structural schemas and then semantic checks.
JSON Schema cannot verify canonical evidence digests, contiguous sequence
intervals, field relationships, unique finding identities, extension declaration
bindings, stream continuity, or privacy. Batches have an 8 MiB encoded limit and
observations have a 1 MiB limit; nested evidence also has depth and array limits.
The producer's reviewed projection controls exported evidence fields. A schema
pass does not certify evidence or permit arbitrary plugin data to become public.

`producer` contains bounded software name and version tokens. Health has exactly
`ready`, `complete`, `mode`, `last_receiver_poll` and `analysis_watermark`. The two
status fields are booleans; mode is `live` or `replay`. Timestamps are finite,
nonnegative Unix seconds or null. A ready live stream requires both timestamps;
the publisher separately checks their age before publication.

An extension envelope contains `namespace`, `schema_version`, and an object
`payload`. Namespaces use lowercase segments separated by periods. Immutable
extension schema versions use `major.minor.patch`. A batch declares the used
schemas through `{namespace, schema_version, sha256}`. The digest identifies the
canonical JSON of a reviewed, locally installed schema. There are at most 64
envelopes per observation and 64 declarations per batch. Namespace uniqueness,
declaration coverage and payload compatibility need semantic validation.

Received declarations cannot supply executable code or remote schema URLs.
Unknown extensions can be stored in the publisher's private inbox, but they stay
quarantined until the configured consumer supports their schema and meaning.
Installing a plugin alone does not authorize its fields for public output.

The initial transport keeps the full change history beginning at sequence 1.
An absent observation never implies withdrawal; a retraction is an explicit
revision. A changed stream or generation requires an explicit recovery process.
This release defines no snapshot message or automatic retention recovery.

`examples/change-batch.example.json` contains constructed aircraft reports and
no actual sighting. It has a valid digest and the current profile, while its
`synthetic: true` marker requires explicit development opt-in. Tests exercise
both its accepted shape and failures that only semantic validation can catch.
