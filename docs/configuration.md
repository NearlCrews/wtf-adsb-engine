# Configuration

The engine reads TOML. Start with [receiver.toml](../examples/receiver.toml) for
live collection or [replay.toml](../examples/replay.toml) for an offline example.
Unknown fields and invalid plugin options fail validation.

Use a private copy of the live configuration so local endpoints do not enter Git:

```sh
install -d -m 0700 state
cp examples/receiver.toml state/receiver.toml
chmod 0600 state/receiver.toml
```

Edit the copy's receiver URL, then validate it:

```sh
.venv/bin/wtf-adsb-engine check --config state/receiver.toml
```

Validation loads selected plugin code and checks configuration. It does not
start plugins, poll the receiver, or prove that a provider is reachable.

## Engine fields

| Field | Meaning |
| --- | --- |
| `version` | Configuration version; currently integer `1`. |
| `state_directory` | Private durable state for one engine process. |
| `source_instance` | Persistent source name matching `[a-z][a-z0-9_-]{0,63}`. |
| `poll_interval_seconds` | Wait between pipeline passes; default 10, permitted range 0.1 to 3600. Work time is additional. |
| `synthetic` | Fixture marker, default false. Keep it true for constructed evidence. |
| `pipelines` | One or more named input/detector pipelines, each with optional ordered enrichers. |
| `outputs` | One or more independently named delivery destinations. |
| `references` | Optional named reference providers. |
| `format` | Optional default format plugin; defaults to `wtf_adsb.json`. |
| `extensions` | Optional locally approved namespace, schema version, and schema path entries. |

Names for pipelines and plugin instances follow the same lowercase pattern as
`source_instance`. Instance names must be unique in their scope. A plugin binding
uses `plugin`, optional `name`, and optional `options`; outputs also require
`name` and `options`, and may override `format`.

The state directory, built-in plugin `path`/`directory` options, and extension
schema paths resolve relative to the TOML file. Shell variables and `~` are not
expanded by the configuration loader. Use literal absolute paths when moving
configuration outside the checkout. External plugins define their own path rules.

## Built-in inputs

| Plugin | Options and behavior |
| --- | --- |
| `wtf_adsb.receiver_service` | Required `url`; optional `poll_seconds` (default 10, range 1 to 60), `timeout_seconds` (default 10, greater than 0 and at most 60), `max_age_seconds` (default 120, range 1 to 3600), `type_registry` and `wind` (both false). Collects continuously into a private journal while export runs separately. |
| `wtf_adsb.receiver_json` | Required `url`; the same HTTP timeout and age options. Fetches one snapshot per pipeline pass. |
| `wtf_adsb.json_replay` | Required `path`; `records_per_poll` defaults to 100 and allows 1 to 1000. Accepts one receiver JSON snapshot per line with strictly increasing `now` timestamps. |
| `wtf_adsb.journal` | Required `path`; `mode` is `live` by default or `replay`. Reads a compatible analysis journal without modifying it. This is not an arbitrary SQLite input. |

Receiver responses must contain a numeric Unix timestamp in `now` and an
`aircraft` array. Live input rejects stale or future timestamps. The engine reads
receiver JSON; it does not decode raw radio samples or Beast/SBS streams.

For the supplied live example, configure native registry and wind enrichment on
`receiver_service`. For `receiver_json` or `json_replay`, the collection detector
also accepts `type_registry` and `wind`. Keep network enrichment disabled for
deterministic offline replay. See [reference data](reference-data.md) for coverage
and attribution limits.

A read-only journal pipeline can use:

```toml
[[pipelines]]
name = "archive"
input = { plugin = "wtf_adsb.journal", options = { path = "/path/to/private/journal-backup.sqlite3", mode = "replay" } }
detectors = [{ plugin = "wtf_adsb.collection" }]
```

This is a pipeline fragment. It still needs the engine fields and output section
from one of the complete examples.

Journal inputs require saved analysis health and a valid recorded timestamp.
Detector errors hold both modes; a receiver collection error also holds live
mode. For replay, any saved archive scan must be explicitly complete. A completed
archive import can use the latest recorded aircraft timestamp even when no live
receiver timestamp exists. An empty archive without a timestamp remains unready.
The collection detector checks health again in the same read transaction as the
exported evidence. A failed check retains the last committed checkpoint.

## Outputs, references, and extensions

The built-in file destination takes `options.directory` and writes
`manifest.json` plus immutable `batches/*.json`. It uses private permissions.
Give a consumer controlled read-only access; do not expose the feed as a public
directory. Each output has its own progress and retry artifacts.

The default `wtf_adsb.json` formatter preserves the entire envelope. SDK v1
formatters may change JSON encoding but cannot discard fields or emit another
wire format.

`wtf_adsb.json_reference` loads a reviewed local JSON object. The
`wtf_adsb.reference_match` enricher uses a configured reference, namespace, and
schema version. For a complete external extension configuration, use the
[reference-label example](../examples/plugins/reference-label/example.toml) with
its separately installed package and reviewed schema.

## Persistent configuration

Keep source, pipeline, and detector instance names stable. Changing input
options or an output destination under an existing persisted name is rejected.
Use a new pipeline name for a changed input and a new output name for a different
destination; a new output starts at sequence 1. Choose live input options before
the first run, since changing them also changes that persisted input binding.
Do not edit identity tables or reset cursors to
bypass these checks.

Keep a replay file immutable after the first run. Its hash is part of the saved
checkpoint. Use separate state for a different recording. New state creates a
new stream and needs explicit consumer reconciliation; it is not transparent
recovery for an existing feed.
