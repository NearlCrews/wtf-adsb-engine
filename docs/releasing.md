# Release maintenance

This guide is for maintainers. The public README installs from a source checkout,
so it does not depend on a package registry release.

## Repository setup

Before the first public push, choose the canonical GitHub repository and add its
real source, issue, and documentation URLs to package metadata where appropriate.
Configure a private security-reporting channel and a private conduct contact;
keep [SECURITY.md](../SECURITY.md) and [CODE_OF_CONDUCT.md](../CODE_OF_CONDUCT.md)
accurate. GitHub private vulnerability reporting requires a repository setting,
not just a file.

Enable the check workflow and review dependency update pull requests. Require
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
`1.0.0` describe different interfaces. Update the relevant version when its
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
check. Record the resulting image ID, build-input hash and package inventory.
Review pinned Python/uv image digests along with library updates, and preserve
dependency license files in derived images. The default workflow builds local
images without publishing or deploying them.

Walk through the README replay from a fresh source directory and verify a
repeat creates no new evidence. Run the installed-wheel and external-plugin
checks. Review documentation links and command help after CLI changes.

Inspect release notes for identity, persistence, and consumer compatibility
changes. State known limitations, including missing reference coverage and
unsupported recovery operations. Record the interpreter and test results used
for the release.

## Publish deliberately

Publishing a repository, creating a release/tag, and uploading a distribution
are separate maintainer actions. The check workflow does not publish packages
or deploy a service. Use the chosen repository's release process after review;
do not invent a registry URL or installation command for a package that has not
been uploaded. Once artifacts are published, verify the downloaded artifacts
and update public installation instructions with the actual release locations.
