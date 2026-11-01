# Setup interview

These are the questions an AI assistant asks before it configures the engine
with `tools/setup.py`. A person can answer them just as well by writing the
answers file by hand. [AGENTS.md](../AGENTS.md) describes the full protocol.

Ask the groups in order, one message per group, and show each default. Leave a
key out of the answers file, or set it to `null`, to accept its default. `python3 tools/setup.py plan`
prints the same questions as JSON.

## Where the answers go

The user names one private directory outside this repository, such as
`/srv/wtf-adsb/config`. It becomes the configuration directory. It holds the
answers file, the generated `receiver.toml`, `.env`, and `setup.json`, and any
provider key file. Compose mounts it read-only at `/config` inside the
container. `configure` creates it, and the state, feed, and reference
directories, with mode 0700. When run with `sudo`, it also gives them to `uid`
and `gid`, and later `check`, `deploy`, and configure runs need `sudo` too.
Every directory must be at least two levels below `/`, even after following
symbolic links, outside this repository, and must not contain it. configure
changes the mode or owner of an existing directory only when it is empty, holds
only the files configure itself writes or the answers name, or already has mode
0700 and the configured owner. Under `sudo` it also refuses an existing
directory that belongs to neither `uid` nor the account that ran `sudo`, root's
included.

The answers file is one JSON object keyed by the names below:

```json
{
  "receiver.url": "http://192.168.1.20:8080/data/aircraft.json",
  "state_dir": "/srv/wtf-adsb/state",
  "feed_dir": "/srv/wtf-adsb/feed",
  "reference_dir": "/srv/wtf-adsb/references",
  "uid": 1000,
  "gid": 1000
}
```

The engine keeps its receiver input options, outputs, and `source_instance`
for the life of the state directory. It refuses to start if they change later.
Settle the Receiver and Providers answers and `source_instance` before the
first start; `uid`, `gid`, and the Runtime answers can change later.
`configure` refuses such a change once state exists.

## Receiver

| Key | Type | Default | Validation | Why |
| --- | --- | --- | --- | --- |
| `receiver.url` | url | none (required) | An `http://` or `https://` URL whose path ends in `aircraft.json`, with no user name or password. Use an address the container can reach; `127.0.0.1` and `localhost` mean the container itself. | The engine polls this readsb- or tar1090-style file for aircraft. |
| `receiver.poll_seconds` | number | `10` | 1 to 60. | How often the collector reads the receiver. |
| `receiver.max_age_seconds` | number | `120` | 1 to 3600. | The collector rejects a snapshot older than this as stale. |

## Directories

| Key | Type | Default | Validation | Why |
| --- | --- | --- | --- | --- |
| `state_dir` | path | `/srv/wtf-adsb/state` | An absolute path at least two levels below `/`, outside this repository and not containing it, using letters, digits, `.`, `_`, `-`, and `/`. It must be separate from the other directories and not inside one of them. | Holds the engine database and the receiver journal. Back it up. |
| `feed_dir` | path | `/srv/wtf-adsb/feed` | The same rules as `state_dir`. | The engine writes `manifest.json` and feed batches here for consumers. |
| `reference_dir` | path | `/srv/wtf-adsb/references` | The same rules as `state_dir`. | `configure` copies the bundled reference tables here, and Compose mounts it read-only. |

## Providers

Every provider is off by default. Turn one on only when the user asks for it.

| Key | Type | Default | Validation | Why |
| --- | --- | --- | --- | --- |
| `type_registry` | boolean | `false` | `true` or `false`. | Checks each aircraft's reported type against the adsbdb.com and hexdb.io registries. It sends aircraft addresses to those services. |
| `wind` | boolean | `false` | `true` or `false`. | Adds Open-Meteo wind data for the area where aircraft fly. It sends rounded positions to Open-Meteo. |
| `flightaware` | boolean | `false` | `true` only after the user says yes to a paid service. | Adds FlightAware AeroAPI flight and owner data. AeroAPI bills the user's own account. |
| `flightaware_key_file` | path | none | Required when `flightaware` is true. A path inside the configuration directory. `check` requires a regular file owned by `uid` with no group or other permissions (0600 or 0400). Neither tool reads the key. | The engine reads the AeroAPI key from this file. Ask for the path only, never the key. |
| `flightaware_daily_budget_usd` | number | `0.5` | 0 to 1000, and no more than the monthly budget. | The engine makes no paid request that would push the UTC day's spending past this amount. |
| `flightaware_monthly_budget_usd` | number | `10` | 0 to 1000. | The same cap for the UTC calendar month. |
| `flightaware_pause_file` | path | none | Optional. A path inside the state or configuration directory. The file does not need to exist. | While this file exists, FlightAware requests pause without reading the key. |
| `fleet_reference_file` | path | none | Optional. A path inside the configuration or reference directory. | A private JSON fleet list that adds fleet context; see [deployment](deployment.md#optional-private-enrichment). |

## Identity

| Key | Type | Default | Validation | Why |
| --- | --- | --- | --- | --- |
| `source_instance` | string | `receiver` | A lowercase letter, then up to 63 lowercase letters, digits, `_`, or `-`. | Names this collector in every batch so consumers can tell sources apart. |
| `uid` | integer | `1000` | 1 to 65534. | The container runs as this user, and it must own the directories. `id -u` shows the user's own number. |
| `gid` | integer | `1000` | 1 to 65534. | The container's group, which must also own the directories. |

## Runtime

| Key | Type | Default | Validation | Why |
| --- | --- | --- | --- | --- |
| `poll_interval_seconds` | number | `60` | 0.1 to 3600. | The wait between analysis and export passes. Collection runs on its own at `receiver.poll_seconds`. |
| `image` | string | `wtf-adsb-engine:local` | A Docker image reference. | The image both services run. `make image` builds the default; a published image tag also works. |
| `memory_limit` | string | `2g` | A whole number followed by `b`, `k`, `m`, or `g`. | The Compose memory limit for the engine. |
| `viewer` | boolean | `true` | `true` or `false`. | Starts the private viewer. It listens on port 8080 inside the Compose network and publishes no host port. When false, deploy stops and removes a running viewer. |
| `viewer_memory_limit` | string | `512m` | The same rules as `memory_limit`. | The Compose memory limit for the viewer. |
