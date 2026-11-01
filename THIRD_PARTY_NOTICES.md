# Third-party notices

The project's original code, documentation, contracts, and examples use the
MIT license in `LICENSE`. Third-party code, dependencies, data, and service
responses retain their own terms. The MIT grant does not replace those terms.
The canonical MIT text is published by the
[Open Source Initiative](https://opensource.org/license/mit).

## Bundled reference material

Reference files are in `src/wtf_adsb_engine/analysis/data/`. Preserve their source
metadata, revisions, hashes, citations, and stated limitations when copying them.

| File | Attribution and recorded source terms |
| --- | --- |
| `ourairports-runways-v1.json` | OurAirports airport and runway data. Its source manifest records public-domain terms and links to <https://ourairports.com/data/>. |
| `reference-beacon-codes-v1.json` | FAA National Beacon Code Allocation Plan and Aeronautical Information Manual. Its source manifest identifies the cited FAA material as US government work. |
| `reference-limits-v1.json` | Facts from cited FAA and EASA type certificate data sheets. Entries retain their source sheets and verification status. The source publications are not relicensed by this project. |
| `reference-envelopes-v1.json` | Project-curated detection thresholds based on published performance figures. Its source notes describe the rounding and limitations; individual values are not cited. |
| `airport-pattern-rule-v1.json` | Project rule configuration, covered by the project's MIT license. |

## Optional sources

The engine does not bundle the three mixed-provenance files
`reference-aircraft-types-v1.json`, `reference-identity-v1.json`, and
`reference-notable-variants-v1.json`. Supplying them through a private reference
directory does not change their redistribution terms.

Optional Mictronics data, Plane Alert DB data, tar1090-derived material, and
Open-Meteo responses have separate source terms and attribution requirements.
The source links and recorded terms are in the repository's
`docs/dependency-audit-2026-10-08.md`. Keep those notices with any supplied data
or derived output that requires them.

## Online providers

Each optional provider is off by default. When it is on, its responses can enter
the journal and, through findings and context, the feed. The operator who turns
a provider on accepts that provider's terms for the deployment and for any feed
consumer that receives its data.

| Provider | Setting | Terms |
| --- | --- | --- |
| [adsbdb.com](https://www.adsbdb.com) | `type_registry` | Aircraft type registry lookups, under the service's own terms. |
| [hexdb.io](https://hexdb.io) | `type_registry` | Aircraft type registry lookups, under the service's own terms. |
| [Open-Meteo](https://open-meteo.com) | `wind` | Responses are licensed under CC BY 4.0 and need attribution. The [service terms](https://open-meteo.com/en/terms) separate free noncommercial access from commercial API access. |
| [FlightAware AeroAPI](https://www.flightaware.com/commercial/aeroapi/) | `flightaware` | A paid service billed to the operator's account. Its terms limit redistribution of responses. |

Installed dependencies carry their own license files. Their licenses and the
locked dependency versions are recorded in the dependency audit and `uv.lock`.
Review source attribution and notices when preparing each public release.

## Container images

The engine image uses the [official Python image](https://hub.docker.com/_/python),
including CPython and Debian system libraries under their respective terms. It
also includes the locked NumPy archive extra and its wheel notices. Installed
Python license files are preserved; Debian copyright and license files remain
under `/usr/share/doc`. `/usr/local/share/wtf-adsb-engine/packages.json` lists
installed packages and Python license paths. The image's MIT label describes
this project's code and does not replace dependency terms.

Build stages use [uv](https://github.com/astral-sh/uv/tree/0.12.24), available under
its [MIT](https://github.com/astral-sh/uv/blob/0.12.24/LICENSE-MIT) or
[Apache-2.0](https://github.com/astral-sh/uv/blob/0.12.24/LICENSE-APACHE) license.
The installer is excluded from the final engine and example plugin images.
Build-image digests and source hashes are recorded in
`/usr/local/share/wtf-adsb-engine/build.json`.
