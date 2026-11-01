# WTF ADS-B Engine

WTF ADS-B Engine collects ADS-B receiver JSON, detects aircraft and flight
patterns, enriches observations, and exports versioned JSON evidence. It supports
live collection and offline replay, with installed plugins for inputs, detection,
reference data, enrichment, formatting, and delivery.

Consumers can use the feed for a website, alerts, or another analysis application
without importing the engine or opening its database. Collection and replay need
no publishing application, paid AI account, or cloud service.

Version 0.1.0 is an early development version. The plugin SDK and evidence
contract have separate versions; see [known limits](#current-limits) before
connecting a persistent consumer.

## Run with Docker

The Linux/amd64 image builds directly from a clean checkout with Docker and GNU
Make. It includes the engine, archive tools and private factual viewer:

```sh
make image
docker run --rm --network none wtf-adsb-engine:local --version
```

The first build downloads pinned base images and locked dependencies. No host
Python installation or prebuilt wheel is required. The
[deployment guide](docs/deployment.md) covers persistent mounts, live
collection, viewer configuration, image checks and derived plugin images. Images
are built locally; this project does not publish to an image registry.

## Try the synthetic replay

Use Linux and [uv](https://docs.astral.sh/uv/getting-started/installation/).
Python 3.14 or newer is required; the pinned and tested interpreter is 3.14.8.
Native Windows is unsupported because the runtime uses POSIX file locking.

Clone this repository using its GitHub Code menu, or extract its source archive,
then change into the project directory. Install and run the offline example:

```sh
uv python install
uv sync --locked
.venv/bin/wtf-adsb-engine --version
.venv/bin/wtf-adsb-engine plugins
.venv/bin/wtf-adsb-engine check --config examples/replay.toml
.venv/bin/wtf-adsb-engine run-once --config examples/replay.toml
.venv/bin/wtf-adsb-engine status --config examples/replay.toml
```

The first install downloads Python and dependencies as needed. The replay itself
makes no provider requests and needs no receiver. It writes private state under
`state/demo` and a manifest with immutable JSON batches under `state/export`.
Its `synthetic: true` marker makes production consumers refuse it. Repeating the
command resumes the saved replay checkpoint; unchanged evidence creates no new
revision. Inspect `state/export/manifest.json` to find the delivered batch files.
Warnings about three omitted reference bundles are expected; see
[reference data and coverage](docs/reference-data.md).

`uv.lock` records exact runtime, development and build dependencies with artifact
hashes. The [dependency audit](docs/dependency-audit-2026-10-08.md) records the
latest stable versions checked on 9 October 2026 and the separate data-source
licensing review.

## Collect from a receiver

Copy [examples/receiver.toml](examples/receiver.toml) to a private configuration:

```sh
install -d -m 0700 state
cp examples/receiver.toml state/receiver.toml
chmod 0600 state/receiver.toml
```

Edit the copy's `url` to point at your receiver's `aircraft.json` endpoint, then:

```sh
.venv/bin/wtf-adsb-engine check --config state/receiver.toml
.venv/bin/wtf-adsb-engine run --config state/receiver.toml
```

That configuration uses `wtf_adsb.receiver_service`: its compatibility collector
processes receiver reports every 10 seconds, while the engine projects the
current journal with a configured 60-second interval between passes. Output
workers deliver committed changes independently. Type-registry and wind requests
are disabled in the example and can be enabled explicitly. The simpler
`wtf_adsb.receiver_json` input polls at the pipeline interval.
`wtf_adsb.json_replay` accepts receiver snapshots as NDJSON;
`wtf_adsb.journal` reads an existing detector journal during migration and holds
export when its saved analysis is incomplete.

Configuration paths are relative to the TOML file. Keep persistent source and
instance names stable, and give each engine process its own state directory.
Use Ctrl-C or SIGTERM to stop. Read [configuration](docs/configuration.md) for
options and [operations](docs/operations.md) for health, backups, upgrades,
retractions, and troubleshooting.

The engine also includes a private factual viewer, optional read-only access to
archived writing, journal migration and a local container build. See
[deployment](docs/deployment.md) for setup and operational checks.

## Analyze archives

The installed engine includes `wtf-adsb-backfill-history`,
`wtf-adsb-backfill-metrics` and `wtf-adsb-import-replay`. They freeze and replay
private historical corpora, analyze captured local metrics, and import compact
replay into a journal backup. Inputs are explicit, and the importer requires
the `archive` extra.
See [archive operations](docs/archive-operations.md) for commands, frozen-source
pinning and private output requirements.

## Extend it

Installed Python packages can register six SDK roles: inputs, detectors,
enrichers, reference providers, formats and outputs. Configuration selects
plugins and separately approves schemas for exported extensions. Discovery reads
package metadata without importing unselected code.

The [reference-label example](examples/plugins/reference-label/README.md) is a
separate installable distribution with a real entry point and an offline
integration test. The [plugin architecture](docs/plugin-architecture.md)
documents the synchronous SDK and its limits. Plugins are trusted Python code
with the engine process's permissions; the SDK is not a security sandbox.

## Evidence and delivery

The implemented interface is `wtf-adsb.collection.v1`, contract version `1.1.0`
(readers also accept `1.0.0` batches).
It exports reviewed collection groups, findings, context and bounded aircraft
tracks. The existing analysis lives behind the compatibility collection plugin;
individual rules have not all become separate plugins.

Read the [evidence format](docs/evidence-format.md), bundled
[JSON Schemas](contracts/README.md) and
[synthetic batch](examples/change-batch.example.json). The durable outbox assigns
stable IDs and revisions, persists batch envelopes and delivery bytes, and keeps
a separate cursor for each output. The file sink writes batches before atomically
replacing their manifest. Delivery is at least once, so consumers must deduplicate.

Receiver coordinates, receiver-relative range and bearing, private paths and
hostnames are excluded from the export profile. Aircraft tracks can still reveal
a general reception region. Keep the feed private and mount it read-only for the
consumer.

## Current limits

This release retains the entire change history. Automatic pruning, recovery
checkpoints, generation recovery and online removal of historical private data
are deferred. Witnessed group merges and splits emit replacement observations
and explicit supersession retractions. Unsupported gaps, ambiguous identity
changes and changed stream generations stop processing. Privacy removal requires
stopping distribution and coordinating sanitized replacement state with every
consumer; adding a retraction alone cannot erase old batches. Bundled airport
coverage is regional, and some reference checks are unavailable without separately
supplied data. The engine does not decode raw radio samples or Beast/SBS streams.

## Contributing and support

```sh
uv sync --locked --extra dev --extra archive
make check
.venv/bin/python tools/check_dependencies.py
```

`make check` runs Ruff, offline unit and integration tests, package builds, and
a synthetic replay from the installed base wheel outside the source checkout.
With Docker available, `make image && make test-image` also checks the actual
image, graceful restart and an independently installed plugin. The image checks
run offline after the build has fetched dependencies.
The separate dependency check uses the live PyPI registry and never updates pins.

Read [CONTRIBUTING.md](CONTRIBUTING.md) before submitting changes. Use
[SUPPORT.md](SUPPORT.md) for questions and [SECURITY.md](SECURITY.md) for private
vulnerability reports. The [documentation index](docs/README.md) links all user
and maintainer guides. See [CHANGELOG.md](CHANGELOG.md) for changes and current
release status.

## License and attribution

The engine's original code, documentation, contracts, and examples use the
[MIT license](LICENSE). Dependencies and reference data retain their separate
terms; see [third-party notices](THIRD_PARTY_NOTICES.md) and
[authors](AUTHORS.md). The engine excludes
`reference-aircraft-types-v1.json`, `reference-identity-v1.json` and
`reference-notable-variants-v1.json` until their redistribution terms are resolved.
Maintainers should follow [release maintenance](docs/releasing.md) when preparing
public distributions.
