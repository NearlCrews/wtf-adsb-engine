# Reference data and optional providers

The engine combines received telemetry with bounded reference information.
A registry or watchlist match describes a database record; it does not confirm
an aircraft's mission, owner at that moment, or occupants.

## Bundled coverage

Five files ship under `wtf_adsb_engine/analysis/data/`:

| File | Coverage and limits |
| --- | --- |
| `ourairports-runways-v1.json` | A regional OurAirports extract bounded by longitudes -93 to -74 and latitudes 38 to 50. It is not a worldwide airport database. |
| `airport-pattern-rule-v1.json` | Pinned runway-circuit rules for that extract and a listed set of fixed-wing types. |
| `reference-beacon-codes-v1.json` | US beacon-code allocations with source editions and interpretation limits. Other jurisdictions can allocate codes differently. |
| `reference-envelopes-v1.json` | Conservative project-curated plausibility thresholds, not certified aircraft operating limits. Individual threshold values are not cited. |
| `reference-limits-v1.json` | Facts from named FAA and EASA type certificate data sheets. A missing type or limit remains unavailable. |

The files retain source metadata and integrity hashes. Preserve them when
redistributing permitted material. [Third-party notices](../THIRD_PARTY_NOTICES.md)
describe the recorded terms.

## Omitted reference bundles

The engine excludes `reference-aircraft-types-v1.json`,
`reference-identity-v1.json`, and `reference-notable-variants-v1.json` while their
mixed-source redistribution terms remain unresolved. Warnings for those missing
files are expected in a clean installation. Available built-in rules continue;
missing checks are not evidence that nothing unusual happened.

`WTF_ADSB_REFERENCE_DIR` can select a private directory containing the exact
reviewed reference files accepted by `analysis/reference.py`. Set it before
starting the engine. It replaces that module's default directory; it does not
merge a few files over the bundled directory. Include the required permitted
bundled files as well when using an override.

Hashes are pinned in the implementation. Editing a JSON file or downloading an
arbitrary newer dataset does not make it accepted reference data. The override
does not replace the separate airport-context dataset. There is no general
reference downloader or data-generator command in this repository.

New reference sources should use the [reference-provider SDK](plugin-architecture.md)
and a reviewed extension schema. Changing a native rule's reference snapshot
requires a code/data review, source terms, coverage notes, hashes, and regression
fixtures. Do not commit privately supplied bundles to a plugin example.

## Network enrichment

The live example disables type-registry and wind requests. Operators can enable
them explicitly in the collection input options described in
[configuration](configuration.md). Network access, provider availability,
attribution, and service terms remain the operator's responsibility.

Frozen archive analysis captures selected reference files and pins its analysis
source. A later environment change must not alter a frozen corpus. Use
[archive operations](archive-operations.md) for that workflow; raw frozen
artifacts remain private and are not interchangeable with validated feed batches.
