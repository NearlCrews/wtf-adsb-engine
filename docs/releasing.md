# Release maintenance

This guide is for maintainers. The public README builds the image from a source
checkout and names the released container image as an alternative; no Python
package is uploaded to a package registry.

## Repository setup

Before the first public push, choose the canonical GitHub repository and
replace the `OWNER` placeholder with its owner in the `Homepage`, `Source`,
`Issues`, and `Changelog` entries of the `[project.urls]` table in
`pyproject.toml`, in the `ghcr.io/OWNER/wtf-adsb-engine` image name in the
README and in this guide, and in the publisher repository link in
[CONTRIBUTING.md](../CONTRIBUTING.md). The `Example deployment` entry links to
wtf-adsb.com and needs no change. That list can go stale, so treat this command
as the authoritative check; afterwards it should show only this guide's own
mentions of the placeholder:

```sh
git grep -nw OWNER
```

Configure a private security-reporting channel and a private conduct contact;
keep [SECURITY.md](../SECURITY.md) and
[CODE_OF_CONDUCT.md](../CODE_OF_CONDUCT.md) accurate. GitHub private
vulnerability reporting is a repository setting that must be turned on
separately from SECURITY.md.

Enable the check and release workflows and review dependency update pull requests. Require
successful checks on the default branch when the repository's settings support
it. The repository includes issue and pull request templates. A CODEOWNERS file
or funding configuration should name actual agreed accounts; neither is needed
to build or run the engine.

Review the intended public tree and its history before pushing. Exclude private
state, original deployment records, receiver-derived fixtures, raw archives,
credentials, and copied private reference bundles. The new repository does not
need the private application's Git history. Preserve authorship and
[third-party notices](../THIRD_PARTY_NOTICES.md); an MIT license does not clear
unresolved data redistribution terms.

## Prepare a version

Application version `0.1.0`, SDK API version `1`, and evidence contract version
`1.1.0` describe different interfaces. Update the relevant version when its
compatibility changes; changing a package version does not authorize a wire
breaking change.

Update `pyproject.toml` and `src/wtf_adsb_engine/__init__.py` together. Inspect
built-in producer/plugin version defaults, documentation examples, and the
example plugin's engine pin when updating a release. Move the appropriate
[Unreleased changelog](../CHANGELOG.md) entries into a dated version section only
when preparing that release.

For dependencies, check current stable upstream releases, review compatibility,
update exact pins, and refresh `uv.lock`. Keep GitHub Actions at verified commit
SHAs and document deliberate compatibility exceptions. Reference data updates
have their own provenance and detector review; they do not follow library tags.

## Validate artifacts

From a clean checkout on the supported platform:

```sh
uv python install
uv sync --locked --extra dev --extra archive
.venv/bin/python tools/check_dependencies.py
UV_OFFLINE=1 make check
make image
make test-image
```

The dependency audit uses the network. The test/build pass uses cached pinned
dependencies. Check both the source archive and wheel in `dist/`: they must
contain the MIT license, notices, schemas, and permitted reference data, with
no runtime state or excluded bundles. The wheel metadata includes the public
README and license expression.

Build the Linux/amd64 container from clean source, then run its offline image
check. Record the resulting image ID, build-input hash, and package inventory.
Review pinned Python/uv image digests along with library updates, and preserve
dependency license files in derived images. The check workflow builds local
images without publishing or deploying them.

Walk through the [synthetic replay](../DEVELOPMENT.md#try-the-synthetic-replay)
from a fresh source directory and verify a repeat creates no new evidence. Run the installed-wheel and external-plugin
checks. Review documentation links and command help after CLI changes.

Inspect release notes for identity, persistence, and consumer compatibility
changes. State known limitations, including missing reference coverage and
unsupported recovery operations. Record the interpreter and test results used
for the release.

## Publish a release

Publishing a repository and pushing a release tag are separate maintainer
actions. The check workflow never publishes. When `main` holds the reviewed
release commit, its checks are green, and `CHANGELOG.md` has a section headed
with the version (such as `## 0.1.0 (2026-11-01)`), tag and push it:

```sh
git tag -a v0.1.0 -m "wtf-adsb-engine 0.1.0"
git push origin v0.1.0
```

The tag starts `.github/workflows/release.yml`, which uses only the workflow's
`GITHUB_TOKEN` with `contents: write` and `packages: write`. It:

1. refuses a tag that is not `v` followed by the `pyproject.toml` version, and
   takes the release notes from the matching changelog section with
   `tools/release_notes.py`;
2. runs `make check`, `make image`, and `make test-image`, and copies
   `build.json` and `packages.json` out of the local image;
3. pushes the image to `ghcr.io/OWNER/wtf-adsb-engine` under the version tag
   (`v0.1.0`), and writes the pushed digest to the job summary and the release
   notes;
4. creates the GitHub release for the tag with those two inventories, the
   source archive, and the wheel attached;
5. tags and pushes the same image as `latest`, only after the release exists
   and only when `packaging` does not report the version as a pre-release
   (`Version(version).is_prerelease`). A pre-release such as `0.2.0rc1`,
   `0.2.0b1`, `0.2.0.dev1`, or `0.2.0-rc.1` never moves `latest`, and a failed
   release leaves it on the previous release.

GitHub derives the image name from the repository, lowercased, so `OWNER` is
the account or organization that holds the repository. Replace the placeholder
wherever it appears once the owner is chosen. A new GHCR package is private
until its visibility is changed in the package settings; make it public after
the first release, then pull the tag and compare its digest with the release
notes.

A run that fails before the push can be rerun from the Actions page. Once the
push has succeeded, do not rerun the workflow: the build is not
byte-reproducible, so a rerun republishes the version tag with a new digest
that no longer matches the one already reported. If only the release creation
failed, create the missing release by hand from that run's outputs instead:
pull the pushed version tag and copy `build.json` and `packages.json` out of
it, write the notes with `tools/release_notes.py` and add the digest from the
run's job summary, build the archives with `make build` from the tagged commit,
attach them with `gh release create --verify-tag`, and then, for a version
that is not a pre-release, tag the pulled image as `latest` and push it. Do not move a
published tag; fix forward with a new version instead.
