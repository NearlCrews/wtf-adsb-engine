# Dependency audit, 8 October 2026, updated 9 October

> Note, 1 November 2026: this audit records the October baseline and is not
> updated with later pins. The current pins are in `pyproject.toml` (and
> `uv.lock`), and the [changelog](../CHANGELOG.md) records each change, such as
> the 31 October move to Python 3.14.9, Ruff 0.17.2, and NumPy 2.5.4.

The engine's direct Python dependencies match the latest stable releases checked
against PyPI on 9 October 2026. The initial audit and reference-data review were
completed on 8 October; the container follow-up updated Ruff and uv. Versions
remain exact pins so that updates are reviewable. The interpreter used for this split is Python 3.14.8, the current
stable release in the [Python release index](https://www.python.org/api/v2/downloads/release/?is_published=true).

| Dependency | Pin | Purpose | Upstream license metadata |
| --- | --- | --- | --- |
| [jsonschema](https://pypi.org/pypi/jsonschema/json) | 4.26.0 | Contract and plugin payload validation | MIT |
| [packaging](https://pypi.org/pypi/packaging/json) | 26.3 | Plugin compatibility and maintenance version checks | Apache-2.0 OR BSD-2-Clause |
| [Hatchling](https://pypi.org/pypi/hatchling/json) | 1.32.4 | Package build backend | MIT |
| [build](https://pypi.org/pypi/build/json) | 1.6.1 | Development package builds | MIT |
| [Ruff](https://pypi.org/pypi/ruff/json) | 0.17.0 | Development lint and formatting | MIT |
| [NumPy](https://pypi.org/pypi/numpy/json) | 2.5.3 | Optional archive tools and development checks | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 |

NumPy 2.5.3 requires Python 3.12 or newer. The engine currently declares Python
3.14 or newer, matching the tested baseline. `jsonschema` uses its base install;
the optional `format` extra adds dependencies with different licenses and is not
implicitly required by the engine.

The maintenance command reads exact pins in `pyproject.toml`, root
`requirements*.txt`, and the two conventional npm manifests when present:

```sh
python tools/check_dependencies.py
python tools/check_dependencies.py --json
```

It makes explicit HTTPS requests to PyPI and npm and never edits or installs
packages. An unavailable registry or an outdated pin returns a failing exit
status. Python comparisons exclude prereleases, yanked releases, and releases
without downloadable files. Offline tests cover those decisions, rejected
unpinned requirements, network failures, npm prereleases, and an explicit Node
compatibility exception. Keep this network command separate from `make check`.

The repository's `uv.lock` records the resolved runtime, development and build
dependency graph, including package artifact hashes. Reproduce it with
`uv sync --locked --extra dev --extra archive`; the `dev` extra includes the
pinned build frontend and backend. The latest transitive versions observed for
the Python build and validation tools were:

| Dependency | Version |
| --- | --- |
| [attrs](https://pypi.org/pypi/attrs/json) | 26.1.0 |
| [jsonschema-specifications](https://pypi.org/pypi/jsonschema-specifications/json) | 2025.9.1 |
| [referencing](https://pypi.org/pypi/referencing/json) | 0.37.0 |
| [rpds-py](https://pypi.org/pypi/rpds-py/json) | 2026.9.1 |
| [pathspec](https://pypi.org/pypi/pathspec/json) | 1.1.1 |
| [pluggy](https://pypi.org/pypi/pluggy/json) | 1.6.0 |
| [tomlkit](https://pypi.org/pypi/tomlkit/json) | 0.15.1 |
| [trove-classifiers](https://pypi.org/pypi/trove-classifiers/json) | 2026.9.21.13 |
| [pyproject-hooks](https://pypi.org/pypi/pyproject-hooks/json) | 1.3.3 |

Keep the lock and direct pins together when updating dependencies. The
direct-dependency checker does not inspect installed environments, lock
completeness, vulnerability advisories, or transitive compatibility. It reports
the latest upstream release even if a future release raises its Python
requirement; review that requirement before updating the pin.

The public CI template also pins verified release commits for
[actions/checkout 7.0.1](https://github.com/actions/checkout/releases/tag/v7.0.1)
and [setup-uv 10.2.0](https://github.com/astral-sh/setup-uv/releases/tag/v10.2.0),
with [uv 0.12.24](https://github.com/astral-sh/uv/releases/tag/0.12.24).
Ruff [0.17.0](https://github.com/astral-sh/ruff/releases/tag/0.17.0) was verified
after its 9 October release. The Dockerfile pins the official Python 3.14.8 and
uv images by registry digest and records the resulting installed package inventory.
Dependency update configuration covers Docker, the uv lock and GitHub Actions.
A successful local check does not establish that a hosted workflow has run; verify its result on GitHub
when preparing a release.

The existing data sources need separate notices and release decisions:

| Material | Distribution treatment |
| --- | --- |
| OurAirports airport/runway facts | The bundled file retains revision, hashes and source links. [OurAirports](https://ourairports.com/data/) publishes these data as public domain. |
| FAA beacon codes and aircraft limits | Bundled compact facts retain the original source editions and citations. The source manifests identify the FAA material as US government work. |
| Curated performance envelopes and EASA limit facts | Bundled factual tables retain per-type sources. Review provenance when changing a table; the source documents are not relicensed by the engine. |
| Mictronics aircraft data | [ODC Attribution License](https://github.com/Mictronics/aircraft-database/blob/main/LICENSE). Preserve attribution if an optional reference provider supplies these data. |
| Plane Alert DB | [ODbL and Database Contents License](https://github.com/sdr-enthusiasts/plane-alert-db/blob/main/LICENSE). Keep database terms and attribution with optional watchlist providers and their output. |
| tar1090 address tables | The upstream [GPL-2.0-or-later license](https://github.com/wiedehopf/tar1090/blob/master/LICENSE) needs an explicit redistribution review. The mixed identity data file is omitted from the bundled engine. |
| tar1090-db-derived type/variant material | The reviewed source inventory recorded no declared standalone license. The affected aircraft-type and notable-variant JSON files are omitted from the bundled engine. |
| Open-Meteo weather responses | The optional adapter must retain CC BY 4.0 attribution. [Service terms](https://open-meteo.com/en/terms) also distinguish free noncommercial access from commercial API access. |

The owner selected MIT for the engine's original code, documentation, contracts,
and examples on 8 October 2026. [LICENSE](../LICENSE) and
[third-party notices](../THIRD_PARTY_NOTICES.md) are included in its distributions.
Review the [authorship](../AUTHORS.md), notices, and distribution contents when
preparing each release. Third-party material retains its own terms. Private reference files can be mounted by an
operator through the documented reference directory; they are not part of the
public distribution. Reference-data updates retain reviewed snapshots and
provenance rather than following library release tags automatically.
