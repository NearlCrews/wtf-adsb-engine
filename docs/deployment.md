# Deployment and journal migration

The engine and its private viewer can run as separate processes from the same
installed wheel. The viewer reads the native journal and accepts explicit
operator feedback; it starts no collector, paid provider or writing worker.
The JSON feed remains the interface for publishing applications.

## Local container build

Use Docker and GNU Make on Linux/amd64. Build from the source checkout:

```sh
make image
docker run --rm --network none wtf-adsb-engine:local --version
```

The multistage Dockerfile pins Python 3.14.9 and uv 0.12.24 by version and digest.
It builds the wheel inside Docker and installs dependencies from `uv.lock`,
including the NumPy archive extra. The final image contains the installed
package, its viewer assets and a real `engine` account with UID/GID 1000. Build
tools and host virtual environments are excluded. The allowlisted build context
excludes private state, credentials and local artifacts.

`make image` retains the build stage as `wtf-adsb-engine:local-build` so plugin
checks can run offline even after temporary test images are removed. Keep that
builder locally while developing plugins; deploy the runtime image only.
Use the same Docker builder for `make image` and `make test-image`. If you switch
builders, rebuild both images so the selected builder has their complete layers.
`wtf-adsb-engine:local` is a local tag. Set `IMAGE` to choose another local tag;
the builder defaults to that tag plus `-build`. Only `linux/amd64` is currently
tested. The default command starts collection
with `run --config /config/receiver.toml`; mount the configured files and state
before starting it. Pass `--help` to inspect commands without starting collection.

After the [developer setup](../DEVELOPMENT.md#set-up-a-checkout),
validate the installed image:

```sh
make check
make test-image
docker image inspect --format '{{.Id}}' wtf-adsb-engine:local
docker run --rm --network none --entrypoint cat wtf-adsb-engine:local \
  /usr/local/share/wtf-adsb-engine/build.json
docker run --rm --network none --entrypoint cat wtf-adsb-engine:local \
  /usr/local/share/wtf-adsb-engine/packages.json
```

`make test-image` runs synthetic replay, repeat delivery, viewer HTTP/assets,
archive dependency loading, liveness/readiness and graceful restart checks with
container networking disabled. It builds and executes the independent example
plugin in a derived image, using its matching builder image and a hashed
wheel. It uses temporary state, removes its test containers/images, and makes no
provider requests. It requires the matching builder image, selected with
`BUILDER_IMAGE` when using a separate tag. Run it as an unprivileged user with
Docker access. It rejects an image whose recorded build inputs differ from the
checkout; rebuild first
after an input change. `IMAGE` must match the tag used for `make image`.

`build.json` records source/lock hashes, wheel hashes and pinned base/build images.
`packages.json` records installed Python and Debian package versions and license
locations. Keep the image ID, these inventories and the matching configuration
with deployment records. Use the verified image ID for `ENGINE_IMAGE` during a
handoff; a local tag can move. Keep the previous image available for recovery.

Plugins remain installed Python distributions. Extend the image with reviewed,
exact wheels and hashes during a build, then run the image checks for that plugin.
The [reference-label example](../examples/plugins/reference-label/README.md#container-image)
includes a derived Dockerfile. Its final image retains the unprivileged account
and refreshes the package inventory; it does not retain its installer. The JSON
contract and configured schema approvals remain the same.

## Persistent deployment

[deploy/compose.yaml](../deploy/compose.yaml) supplies engine and viewer service
templates. Set `ENGINE_CONFIG_DIR`, `ENGINE_STATE_DIR`, `ENGINE_FEED_DIR` and
`ENGINE_REFERENCE_DIR` to absolute private directories. The configuration file
must be named `receiver.toml`; use `/state` and `/feed` for its container paths.
Make writable directories owned by `ENGINE_UID:ENGINE_GID` (defaults 1000:1000).
Mount provider keys read-only only when their provider is enabled.
Mount the feed read-only into consumers. Keep the entire state directory and
feed on persistent local storage; replacing a container must retain their
identity, journal, accounting, locks and delivery cursors. Use the same image ID
for the engine and viewer. The viewer needs writable access to the native
journal for explicit operator feedback, while a saved-writing archive has its
own read-only mount.

Both services give `/tmp` a 512 MiB tmpfs because the root filesystem is
read-only and SQLite writes its temporary sort files there when it orders the
retained journal (collection export and the viewer's selection views). With
64 MiB a full journal fails with "database or disk is full". Tmpfs pages count
toward the container's memory limit; to keep them out of memory, set
`SQLITE_TMPDIR` to a directory on the state volume instead.

```sh
docker compose -f deploy/compose.yaml config --quiet
docker compose -f deploy/compose.yaml up -d
```

Configure the receiver network in the parent Compose project. This template
neither creates receiver networks nor exposes host ports. Its viewer port 8080
belongs behind your authenticated private network or reverse proxy. Public
websites should consume the reviewed evidence feed, not the private viewer API.

The default limits are 2 GiB for collection and 512 MiB for the viewer. Measure
initial export and steady-state peak memory with your retained history; increase
the limits before activation if needed. Full historical projection may take
several minutes. Set shutdown grace longer than the measured worst pass, and
verify that steady-state analysis stays within the consumer freshness window.

Docker restarts use process liveness. Gate consumer activation on
`health --config /config/receiver.toml --readiness` and a fresh delivered feed;
initial export can remain unready while the process is healthy. Test initial
sync and a fresh subsequent pass before starting an observation window. Preserve
the old deployment until the chosen window and rollback rehearsal pass.

## Copy a compatible journal

Prepare a `wtf_adsb.receiver_service` configuration with a new state directory.
Stop the previous collector and its enrichment/writing workers before the final
handoff. A live SQLite backup is suitable for rehearsals, but it does not transfer
later observations or spending reservations.

```sh
.venv/bin/wtf-adsb-engine migrate-journal \
  --source /private/previous/journal.sqlite3 --config state/receiver.toml
.venv/bin/wtf-adsb-engine run --config state/receiver.toml
```

Use `--pipeline NAME` if the configuration contains several native collectors.
Migration takes the engine lock, requires empty destination state, uses SQLite's
backup API, checks database integrity and records a private receipt. Observations,
reviews, provider caches and accounting rows are retained. Providers are not
started by the migration command. An interrupted copy leaves a marker that blocks
normal startup. Preserve the failed destination for diagnosis and retry into a
fresh directory; do not remove the marker to bypass verification.

Keep old code and a consistent backup until the consumer has caught up and the
new deployment has passed its observation period. Engine and consumer rollback
must preserve current sequence positions and external side effects. See
[operations](operations.md#state-backups-and-upgrades).

## Private viewer and saved writing

Start the factual viewer on loopback:

```sh
.venv/bin/wtf-adsb-engine serve --config state/receiver.toml
```

Open `http://127.0.0.1:8080/wtf/`. `--host`, `--port` and `--pipeline` select its
listener and native journal. `--map-url` optionally accepts a private map page
prefix to which aircraft links are appended. Without it, no local map link is
shown. `/wtf/metrics` preserves the detector metrics; `/wtf/health` reports viewer
availability while summary data reports receiver freshness.

To retain legacy AI notes and drafts, make a separate, verified, standalone
SQLite backup with no outstanding WAL and record its capture time as Unix
seconds. Mount it read-only, separately from the active journal:

```sh
.venv/bin/wtf-adsb-engine serve --config state/receiver.toml \
  --archive /private/archive/saved-writing.sqlite3 --captured-at 1791000000
```

Saved writing appears only under `/wtf/archive/` and `/wtf/api/archive/`.
Archive date filters use the capture time, so records remain visible later.
Archive feedback is refused; current provider and fleet overlays are omitted.
The active viewer shows facts and current reviews without generating or exposing
saved drafts. Retained writing is historical prose, not newly validated evidence.

## Optional private enrichment

The native input supports explicit `flightaware`,
`flightaware_key_file`, `flightaware_daily_budget_usd` and
`flightaware_monthly_budget_usd` settings. It is disabled by default and runs at
most once per 300 seconds when enabled. Keep its journal accounting through
migration; resetting that database would reset its recorded reservations.

Set `flightaware_pause_file` to a persistent private path before a handoff.
While that file exists, both scheduled and direct enrichment return `paused`
without reading credentials or reserving paid requests. A dangling symlink also
holds requests. Create the hold before starting the enabled production collector,
then remove it only after recording the external-activation commitment. This
changes no durable input options. A pause does not cancel an already running
request; stop and drain the previous worker before transferring accounting.

`fleet_reference_file` may point to a private JSON file with `checked_on` in
`YYYY-MM-DD` form and an `aircraft` object keyed by registration. Each entry has
`aircraft_type`, `organization`, `association` and an HTTPS `source_url`. Files
are bounded to 1 MiB. Matching references are treated as current for 30 days;
missing or invalid files leave factual pages available. A fleet listing does
not establish who operated a particular flight or why it flew.
