# WTF ADS-B Engine

WTF ADS-B Engine reads the aircraft JSON from an ADS-B receiver (readsb- or
tar1090-style) and detects notable aircraft and flight patterns. Each finding
that passes the engine's own checks goes to a versioned JSON feed with its
evidence attached. A website, alert script, or other program can read that
feed without importing the engine or opening its database. It needs no cloud
service or paid account.

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
and the readiness command shown below.

## Quick start with Docker

You need Linux on x86-64 (amd64), Docker with Compose, GNU Make, and a receiver
whose `aircraft.json` the Docker host can reach. Only x86-64 is built and
tested. Clone this repository, change into it, and build the image:

```sh
make image
```

After the first release, tagged images are also on the GitHub Container
Registry. Instead of `make image` you can then run
`docker pull ghcr.io/OWNER/wtf-adsb-engine:v0.1.0` and add
`ENGINE_IMAGE=ghcr.io/OWNER/wtf-adsb-engine:v0.1.0` to the `engine.env` file
below.

Create a parent directory your account owns, a configuration directory your
account owns with group 1000, and three private directories owned by UID and
GID 1000, the image's default account. The container reads the configuration
through its group but cannot change it. Any absolute path works in place of
`/srv/wtf-adsb`.

```sh
sudo install -d -m 0755 -o "$(id -u)" -g "$(id -g)" /srv/wtf-adsb
sudo install -d -m 0750 -o "$(id -u)" -g 1000 /srv/wtf-adsb/config
sudo install -d -m 0700 -o 1000 -g 1000 /srv/wtf-adsb/state /srv/wtf-adsb/feed /srv/wtf-adsb/references
sudo install -m 0640 -o "$(id -u)" -g 1000 examples/receiver.toml /srv/wtf-adsb/config/receiver.toml
sudo install -m 0644 -o 1000 -g 1000 src/wtf_adsb_engine/analysis/data/*.json /srv/wtf-adsb/references/
```

Edit `/srv/wtf-adsb/config/receiver.toml` and change three values:

- `state_directory = "/state"`
- the input's `url`, set to your receiver's `aircraft.json`, such as
  `http://192.168.1.20:8080/data/aircraft.json`. Inside the container,
  `127.0.0.1` is the container itself.
- the output's `directory = "/feed"`

Write these four lines to `/srv/wtf-adsb/engine.env`:

```sh
ENGINE_CONFIG_DIR=/srv/wtf-adsb/config
ENGINE_STATE_DIR=/srv/wtf-adsb/state
ENGINE_FEED_DIR=/srv/wtf-adsb/feed
ENGINE_REFERENCE_DIR=/srv/wtf-adsb/references
```

Start the services and check readiness:

```sh
docker compose --env-file /srv/wtf-adsb/engine.env -f deploy/compose.yaml up -d
docker compose --env-file /srv/wtf-adsb/engine.env -f deploy/compose.yaml exec engine \
  wtf-adsb-engine health --config /config/receiver.toml --readiness
```

Readiness reports not ready until the first export finishes, which can take
several minutes. Warnings about three missing reference bundles are expected;
see [reference data](docs/reference-data.md). The
[deployment guide](docs/deployment.md#compose-settings-viewer-and-upgrades)
covers optional settings, the private viewer, and upgrades.

## Have an AI assistant set it up

Open this repository in Claude Code or a similar harness and say "set this up."
[AGENTS.md](AGENTS.md) tells the assistant what to ask you, where to keep your
private configuration, and how to check and start the service. Its setup tool
writes the same layout as above. When the container's account is not yours, it
runs with `sudo` and keeps the configuration with your account.

## More

- Run without Docker: [DEVELOPMENT.md](DEVELOPMENT.md#run-without-docker).
- What the feed holds and leaves out: [where the data goes](DEVELOPMENT.md#where-the-data-goes).
- Guides for configuration, operations, deployment, reference data, and the
  plugin SDK: the [documentation index](docs/README.md). Development and
  releases: [DEVELOPMENT.md](DEVELOPMENT.md).
- Help: [SUPPORT.md](SUPPORT.md). Vulnerability reports: [SECURITY.md](SECURITY.md).
- License: the engine's code, documentation, contracts, and examples use the
  [MIT license](LICENSE). Dependencies and reference data keep their own
  terms; see the [third-party notices](THIRD_PARTY_NOTICES.md) and
  [authors](AUTHORS.md).
