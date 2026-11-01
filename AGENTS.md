# Engine development

- Keep the installable package in `src/wtf_adsb_engine/`, tests in `tests/`, public contract assets in `contracts/`, and runnable examples in `examples/`.
- This project collects, analyzes, and exports factual aircraft evidence. Site policy, article writing, themes, and publication belong to downstream consumer applications.
- Run `make check`. Tests must work offline and must not spend AI or paid-provider credits.
- Keep public instructions self-contained. Read `CONTRIBUTING.md`; update the README, relevant guides, and changelog for user-visible changes. Do not invent repository URLs, contacts, release dates, or supported platforms.
- Use exact, verified current stable dependency pins and update `uv.lock` with dependency changes. The read-only version audit is `python tools/check_dependencies.py`; it uses the network and is separate from offline tests.
- Preserve stable identities, durable revision/cursor transactions, immutable delivery retries, source provenance, and explicit retractions. Missing retained observations never mean deletion.
- Build export data from reviewed field allowlists. Exclude receiver coordinates, receiver-relative measurements, private hostnames, credentials, journal prose, and local/archive paths. Aircraft tracks are allowed by the collection profile.
- Installed plugins are trusted code. Do not describe in-process plugin interfaces as a security sandbox.
- Keep all runtime state out of version control. Never add live databases, raw archives, or credentials. If `.private/` is used, keep it mode 0700.
- The engine's original code, documentation, contracts, and examples use MIT. Preserve `LICENSE`, third-party notices, and separate data-source terms. Review packaging and third-party data provenance before public release.
- Do not create a Git remote or publish this repository unless requested.
