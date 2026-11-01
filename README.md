# WTF ADS-B Engine

WTF ADS-B Engine reads the aircraft JSON from an ADS-B receiver (readsb or
tar1090 style), detects notable aircraft and flight patterns, and writes
reviewed findings to a versioned JSON feed. A website, alert script, or other
program can read that feed without importing the engine or opening its
database. It needs no cloud service or paid account.

[wtf-adsb.com](https://wtf-adsb.com) is an example deployment: a separate
publisher turns this engine's feed into stories.

Version 0.1.0 is an early release. Read the
[current limits](DEVELOPMENT.md#current-limits) before you connect a
long-running consumer.

## Setup and go

You configure the engine once and start the container. From then on it polls
the receiver, keeps its own journal, and delivers new findings to the feed
directory without supervision. Docker restarts it after a crash or reboot and
runs its health check every 30 seconds. Upkeep is an occasional image upgrade
and the health command shown below.

## Quick start with Docker

You need Linux, Docker with Compose, GNU Make, and a receiver whose
`aircraft.json` the Docker host can reach. Clone this repository, change into
it, and build the image:

```sh
make image
```

Create four private directories owned by UID and GID 1000, the image's default
account. This example uses `/srv/wtf-adsb`; any absolute path works.

```sh
sudo install -d -m 0700 -o 1000 -g 1000 \
  /srv/wtf-adsb/config /srv/wtf-adsb/state /srv/wtf-adsb/feed /srv/wtf-adsb/references
sudo install -m 0600 -o 1000 -g 1000 examples/receiver.toml /srv/wtf-adsb/config/receiver.toml
sudo install -m 0644 -o 1000 -g 1000 src/wtf_adsb_engine/analysis/data/*.json /srv/wtf-adsb/references/
```

Compose points the engine's reference tables at the references directory, so
the last command copies the bundled tables there. Edit
`/srv/wtf-adsb/config/receiver.toml` (with `sudo` unless your account is UID
1000) and change three values:

- `state_directory = "/state"`
- the input's `url`, set to your receiver's `aircraft.json`, such as
  `http://192.168.1.20:8080/data/aircraft.json`. Inside the container,
  `127.0.0.1` is the container itself, so use an address on your network.
- the output's `directory = "/feed"`

Write the Compose settings to `/srv/wtf-adsb/engine.env`:

```sh
ENGINE_CONFIG_DIR=/srv/wtf-adsb/config
ENGINE_STATE_DIR=/srv/wtf-adsb/state
ENGINE_FEED_DIR=/srv/wtf-adsb/feed
ENGINE_REFERENCE_DIR=/srv/wtf-adsb/references
```

Optional settings are `ENGINE_IMAGE` (default `wtf-adsb-engine:local`),
`ENGINE_UID` and `ENGINE_GID` (default 1000), and `ENGINE_MEMORY_LIMIT`
(default `2g`). Start the services and check readiness:

```sh
docker compose --env-file /srv/wtf-adsb/engine.env -f deploy/compose.yaml up -d
docker compose --env-file /srv/wtf-adsb/engine.env -f deploy/compose.yaml exec engine \
  wtf-adsb-engine health --config /config/receiver.toml --readiness
```

The first export can take several minutes, and readiness reports not ready
until it finishes. Compose also starts a private viewer on port 8080 inside the
Compose network. It publishes no host port; keep it behind your own private
network or an authenticated proxy.

To upgrade, update the checkout, run `make image`, and rerun the `up -d`
command. The state and feed directories carry over.

## Run without Docker

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then:

```sh
uv python install
uv sync --locked
install -d -m 0700 state
cp examples/receiver.toml state/receiver.toml
chmod 0600 state/receiver.toml
```

Set the `url` in `state/receiver.toml`, then run
`.venv/bin/wtf-adsb-engine check --config state/receiver.toml` and
`.venv/bin/wtf-adsb-engine run --config state/receiver.toml`. Use your service
manager to keep it running. To try the engine without a receiver, run
`.venv/bin/wtf-adsb-engine run-once --config examples/replay.toml`.

## Have an AI assistant set it up

Open this repository in Claude Code or a similar harness and say "set this up."
[AGENTS.md](AGENTS.md) tells the assistant what to ask you, where to keep your
private configuration, and how to check and start the service.

## Where the data goes

The feed directory holds `manifest.json` and immutable JSON batches in the
`wtf-adsb.collection.v1` format. Give consumers read-only access and have them
deduplicate, because delivery is at least once. The feed leaves out receiver
coordinates, receiver-relative range and bearing, and private hostnames, but
aircraft tracks can still suggest the general reception area. See the
[evidence format](docs/evidence-format.md).

## Documentation

The [documentation index](docs/README.md) covers
[configuration](docs/configuration.md), [operations](docs/operations.md),
[deployment](docs/deployment.md), [reference data](docs/reference-data.md),
and the [plugin SDK](docs/plugin-architecture.md). For help, read
[SUPPORT.md](SUPPORT.md); report vulnerabilities through
[SECURITY.md](SECURITY.md).

## Development

[DEVELOPMENT.md](DEVELOPMENT.md) covers the development setup, checks, image
builds, plugins, and releases.

## License

The engine's code, documentation, contracts, and examples use the
[MIT license](LICENSE). Dependencies and reference data keep their own terms;
see the [third-party notices](THIRD_PARTY_NOTICES.md) and [authors](AUTHORS.md).
