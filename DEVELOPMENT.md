# Development

This guide is for people who change the engine, write plugins, or prepare
releases. To install and run the engine, start with the [README](README.md).

## Set up a checkout

Use Linux, Git, GNU Make, and
[uv](https://docs.astral.sh/uv/getting-started/installation/). Python 3.14 or
newer is required; the pinned and tested interpreter is 3.14.9, recorded in
`.python-version`. Native Windows is unsupported because the runtime uses POSIX
file locking.

Clone this repository using its GitHub Code menu, or extract its source archive,
then change into the project directory:

```sh
uv python install
uv sync --locked --extra dev --extra archive
```

The first install downloads Python and dependencies as needed. `uv.lock`
records exact runtime, development, and build dependencies with artifact
hashes. The [dependency audit](docs/dependency-audit-2026-10-08.md) records the
latest stable versions checked on 9 October 2026 and the separate data-source
licensing review.

## Run the checks

```sh
make check
.venv/bin/python tools/check_dependencies.py
```

`make check` runs Ruff, the documentation link check, offline unit and
integration tests, package builds, and a synthetic replay from the installed
base wheel outside the source checkout. It needs no receiver, Docker, cloud
account, or API key. After setup, `UV_OFFLINE=1 make check` also verifies that
package installation uses the local dependency cache. The separate dependency
check uses the live PyPI registry and never updates pins.

[CONTRIBUTING.md](CONTRIBUTING.md) lists the individual targets (`make lint`,
`make test`, `make test-wheel`, and `make docs`) and how to run one test module.

## Try the synthetic replay

After the base install (`uv sync --locked` is enough), run the offline example:

```sh
.venv/bin/wtf-adsb-engine --version
.venv/bin/wtf-adsb-engine plugins
.venv/bin/wtf-adsb-engine check --config examples/replay.toml
.venv/bin/wtf-adsb-engine run-once --config examples/replay.toml
.venv/bin/wtf-adsb-engine status --config examples/replay.toml
```

The replay makes no provider requests and needs no receiver. It writes private
state under `state/demo` and a manifest with immutable JSON batches under
`state/export`. Its `synthetic: true` marker makes production consumers refuse
it. Repeating the command resumes the saved replay checkpoint; unchanged
evidence creates no new revision. Inspect `state/export/manifest.json` to find
the delivered batch files. Warnings about three omitted reference bundles are
expected; see [reference data and coverage](docs/reference-data.md).

## Build and check the image

The Linux/amd64 image builds directly from a clean checkout with Docker and GNU
Make. It includes the engine, the archive tools, and the private factual viewer:

```sh
make image
make test-image
docker run --rm --network none wtf-adsb-engine:local --version
```

The first build downloads pinned base images and locked dependencies. No host
Python installation or prebuilt wheel is required. `make test-image` checks the
actual image, graceful restart, and an independently installed plugin. It runs
offline after the build has fetched dependencies. Images are built locally.

The [deployment guide](docs/deployment.md) covers builder images, build and
package inventories, persistent mounts, live collection, viewer configuration,
and derived plugin images.

## Analyze archives

The installed engine includes `wtf-adsb-backfill-history`,
`wtf-adsb-backfill-metrics`, and `wtf-adsb-import-replay`. They freeze and
replay private historical corpora, analyze captured local metrics, and import
compact replay into a journal backup. Inputs are explicit, and the importer
requires the `archive` extra. See [archive operations](docs/archive-operations.md)
for commands, frozen-source pinning, and private output requirements.

## Extend it

Installed Python packages can register six SDK roles: inputs, detectors,
enrichers, reference providers, formats, and outputs. Configuration selects
plugins and separately approves schemas for exported extensions. Discovery reads
package metadata without importing unselected code.

The [reference-label example](examples/plugins/reference-label/README.md) is a
separate installable distribution with a real entry point and an offline
integration test. The [plugin architecture](docs/plugin-architecture.md)
documents the synchronous SDK and its limits. Plugins are trusted Python code
with the engine process's permissions; the SDK is not a security sandbox.

## Evidence and delivery

The implemented interface is `wtf-adsb.collection.v1`, contract version `1.1.0`
(readers also accept `1.0.0` batches). It exports reviewed collection groups,
findings, context, and bounded aircraft tracks. The existing analysis lives
behind the compatibility collection plugin; individual rules have not all
become separate plugins.

Read the [evidence format](docs/evidence-format.md), the bundled
[JSON Schemas](contracts/README.md), and the
[synthetic batch](examples/change-batch.example.json). The durable outbox
assigns stable IDs and revisions, persists batch envelopes and delivery bytes,
and keeps a separate cursor for each output. The file sink writes batches before
atomically replacing their manifest. Delivery is at least once, so consumers
must deduplicate.

Receiver coordinates, receiver-relative range and bearing, private paths, and
hostnames are excluded from the export profile. Aircraft tracks can still reveal
a general reception region. Keep the feed private and mount it read-only for the
consumer.

## Shared files

The publisher carries copies of `wire.py`, `analysis/kinds.py`,
`analysis/enrichment_rules.py`, and the three JSON Schemas. Both repositories
must hold byte-identical copies. `sync-state.json` records the SHA-256 digest of
each one, and `tests/test_shared_digests.py` fails when a file no longer matches
its recorded digest. Change a shared file in both repositories in the same
release and update the recorded digests with it.

## Current limits

This release retains the entire change history. Automatic pruning, recovery
checkpoints, generation recovery, and online removal of historical private data
are deferred. Witnessed group merges and splits emit replacement observations
and explicit supersession retractions. Unsupported gaps, ambiguous identity
changes, and changed stream generations stop processing. Privacy removal
requires stopping distribution and coordinating sanitized replacement state with
every consumer; adding a retraction alone cannot erase old batches. Bundled
airport coverage is regional, and some reference checks are unavailable without
separately supplied data. The engine does not decode raw radio samples or
Beast/SBS streams.

The engine excludes `reference-aircraft-types-v1.json`,
`reference-identity-v1.json`, and `reference-notable-variants-v1.json` until
their redistribution terms are resolved.

## Release steps

Application version, plugin SDK version, and evidence contract version change
separately. [Release maintenance](docs/releasing.md) covers version updates,
dependency review, artifact validation, and publishing. Record user-visible
changes in [CHANGELOG.md](CHANGELOG.md).

## Contributing and support

Read [CONTRIBUTING.md](CONTRIBUTING.md) before submitting changes. Use
[SUPPORT.md](SUPPORT.md) for questions and [SECURITY.md](SECURITY.md) for
private vulnerability reports. The [documentation index](docs/README.md) links
all user and maintainer guides.
