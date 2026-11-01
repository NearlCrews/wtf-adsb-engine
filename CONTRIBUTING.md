# Contributing

Bug reports, documentation fixes, and plugins are welcome. Read the
[code of conduct](CODE_OF_CONDUCT.md) and use [private reporting](SECURITY.md)
for vulnerabilities or disclosures of private receiver data.

## Set up a checkout

Use Linux, Git, GNU Make, and [uv](https://docs.astral.sh/uv/getting-started/installation/).
Clone this repository using its GitHub Code menu, then change into the checkout.
The pinned interpreter is in `.python-version`.

```sh
uv python install
uv sync --locked --extra dev --extra archive
make check
```

The initial install downloads Python and dependencies as needed. The checks use
synthetic fixtures, local temporary directories, and mocked providers. They need
no receiver, publisher, Docker, browser, cloud account, or API key. After setup,
`UV_OFFLINE=1 make check` also verifies that package installation uses the local
dependency cache.

Useful individual checks:

```sh
make lint
make test
make test-wheel
make docs
.venv/bin/python -m unittest discover -s tests -p 'test_runtime.py'
```

`make test-wheel` builds the source distribution and wheel, installs the base wheel
into a temporary environment, and replays synthetic input outside the checkout.
Keep only the current engine version's wheel in `dist/` when running that check.

For container changes, also run:

```sh
make image
make test-image
```

The build downloads pinned images and locked dependencies. The subsequent image
check runs offline against temporary synthetic state, including an installed
plugin in a derived image. Docker access is required for these two targets; it
remains optional for `make check`. CI runs both sets of checks. See
[deployment](docs/deployment.md#local-container-build) for image inventories and
supported platforms.

## Make a change

Describe the behavior or problem before proposing an implementation. For a new
integration, start with the [plugin SDK](docs/plugin-architecture.md) and
[independent example](examples/plugins/reference-label/README.md). Plugins use
the public SDK rather than importing private analysis modules or writing core
database tables.

Keep changes focused. Add regression tests for changed runtime behavior,
especially identity, retries, checkpoint recovery, evidence validation, and
delivery. Documentation-only edits normally need link checks and a walkthrough
of changed commands rather than new unit tests. Run `make check` before sending
code or configuration changes.

Fixtures must be constructed examples. Do not submit raw receiver archives,
journals, provider credentials, private hostnames, or actual receiver locations.
A finding should distinguish received telemetry, metadata matches, derived
calculations, and uncertain interpretations.

Update user-facing instructions with behavior changes and add a concise entry
to [CHANGELOG.md](CHANGELOG.md). Changes to the wire contract, plugin SDK, or
persisted state need compatibility and recovery notes. Application, SDK, and
contract versions are separate; see [release maintenance](docs/releasing.md).

`wire.py` is shared with the publisher and must stay byte-identical with its copy
(`tests/test_shared_digests.py`). The compiled observation validator leaves
`jsonValue`, `context.novelty`, and `media` to Python checks with the published
rules. `tests/test_wire_validator.py` compares its verdicts with the complete
schema over deterministic mutations, and `tools/fuzz_wire.py` does the same over
seeded random mutations of the example batch, or of a feed directory given with
`--feed`; it is offline and exits with status 1 on any disagreement.

## Dependencies and reference data

Use exact dependency pins and update `uv.lock` with the manifest. Check current
stable upstream versions using:

```sh
.venv/bin/python tools/check_dependencies.py
```

That command uses the network and makes no changes. Explain any upstream
compatibility constraint in the dependency audit instead of forcing an
unsupported version. Run the full checks after an update.

Reference data needs source attribution, redistribution terms, hashes, coverage,
and relevant regression fixtures. See [reference data](docs/reference-data.md).
A newer source snapshot is not automatically a reviewed detector definition.

## Pull requests and licensing

Use the pull request template to explain the problem, resulting behavior, and
checks run. Do not include private logs or state in screenshots and attachments.
Maintainers may request changes or decline additions outside the engine's scope.

Contributions to the project's original code and documentation use its
[MIT license](LICENSE). Preserve existing copyright and third-party notices,
and identify any material that needs different terms. No separate contributor
license agreement or sign-off process is required by this repository.
