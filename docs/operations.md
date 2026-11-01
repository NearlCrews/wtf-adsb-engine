# Operating the engine

Install from the [README](../README.md) and prepare a private
[configuration](configuration.md). Run the live collector in the foreground:

```sh
.venv/bin/wtf-adsb-engine run --config state/receiver.toml
```

Use Ctrl-C or SIGTERM for a graceful stop. For unattended operation, configure
your host's service manager to invoke the same command with absolute executable
and configuration paths. Use an unprivileged service account, private state
storage, and only the network access required by selected inputs and providers.
You can also build the bundled [container definition](../Dockerfile) locally;
see [deployment and migration](deployment.md). No published image or automatic
service installer is provided.

Container deployments keep the engine and viewer on the same tested image ID.
Images include archive dependencies and start as UID/GID 1000 by default. Keep
configuration and provider keys read-only, preserve the full state and feed
directories between containers, and give downstream consumers read-only feed
access. [Deployment](deployment.md#persistent-deployment) describes the mounts
and records needed for an upgrade.

## Commands and health

| Command | Purpose |
| --- | --- |
| `--version` | Print the package version. |
| `plugins` | List installed plugin metadata without importing plugin modules. |
| `check --config PATH` | Validate configuration and construct selected plugins without starting them. |
| `run-once --config PATH` | Process one input batch per pipeline and attempt delivery. A long replay may need more passes. |
| `run --config PATH` | Collect and deliver continuously until stopped. |
| `status --config PATH` | Read persisted core state without starting plugins or taking the service lock. |
| `health --config PATH` | Check process heartbeat, input progress and the active state lock. |
| `health --config PATH --readiness` | Require fresh, complete live analysis and caught-up outputs. |
| `migrate-journal --source PATH --config PATH` | Copy a compatible journal into fresh native collector state without starting providers. |
| `serve --config PATH` | Serve the private factual viewer and feedback API on loopback port 8080. |
| `retract --config PATH --event-id UUID --reason invalid_evidence` | Queue an explicit invalid-evidence retraction while the service is stopped. |

`run-once` returns a nonzero exit status for reported pipeline or output errors.
Normal CLI errors report exception types to avoid exposing credentials from
provider error messages. Keep detailed local diagnostics private.

Inspect health while the service is running:

```sh
.venv/bin/wtf-adsb-engine status --config state/receiver.toml
```

A healthy live feed has ready and complete analysis, a recent receiver poll,
and a recent analysis watermark. A destination's manifest also reflects its
delivery backlog. Process uptime alone does not establish fresh evidence.
Consumers choose their freshness window and must hold dependent work on stale
or incomplete data.

Liveness and readiness serve different purposes. Collection and the independent
heartbeat can remain healthy while a large first export catches up. Use liveness
for restart supervision and readiness for consumer gates. Both commands are
read-only and start no input or provider. `--max-age` defaults to 300 seconds.

Output workers retry temporary database and destination failures independently.
If a failure also prevents saving delivery status, the engine logs the output
name and exception type and keeps retrying. Check the delivered manifest and
cursor as well as saved error status when diagnosing an output that has stopped
advancing.

Each mutating command takes an exclusive lock for its state directory.
Run only one process against that directory. Each output destination must also
have one writer.

## State, backups, and upgrades

Back up the complete configured state directory and its delivered output.
The core database, plugin journals, private identity sidecars, and output
cursors together preserve continuity. A copy of only `engine.sqlite3` is
insufficient.

For a consistent whole-engine backup, stop the process gracefully, copy state,
configuration, reviewed external references, and output into a private backup
directory, then restart. Preserve permissions. SQLite's backup API can capture
an individual live database, but separate live database backups do not form an
atomic whole-engine snapshot. Never copy a live SQLite main file without its WAL
and call that a complete backup.

Before upgrading, read the changelog and recovery notes, preserve the previous
executable and a consistent backup, and validate the new configuration.
Catch up consumers and check feed continuity afterward. Do not restore an older
producer snapshot while consumers still hold newer sequence positions. Recovery
across rollback, changed generations, or lost history needs coordinated consumer
state and is not automated in this version.

## Retention and retractions

The file transport retains all batches from sequence 1. Monitor disk usage and
manifest size; files and manifests have an 8 MiB read bound. Automatic pruning
and recovery snapshots are not implemented. Deleting delivered batches breaks
the contract for existing and future consumers.

Stop the engine before using `retract`. Use an actual exported event UUID and
one of `invalid_evidence`, `superseded`, or `out_of_scope`. The command records
the withdrawal in existing state without loading plugins or polling inputs;
restart the service or run another pass to deliver it.
Automatic witnessed merges and splits include replacement IDs through the
detector SDK. The manual CLI does not accept replacement IDs.

Missing journal rows and an empty poll never retract previously exported
evidence. A retraction also cannot erase old copies. If private data enters the
history, stop distribution and coordinate removal from retained producer,
consumer, and backup artifacts. A new directory alone is not a privacy purge.

## Troubleshooting

| Symptom | What to check |
| --- | --- |
| Missing `aircraft-types`, `identity`, or `notable-variants` reference warnings | These three bundles are intentionally omitted. Built-in fallback rules continue; see [reference data](reference-data.md). |
| A second process fails with `ValueError` | Check whether the state directory is already owned by a running process. Stop it before running a mutating command. |
| Live input is stale | Check the receiver endpoint, receiver and engine clocks, connectivity, and configured age bound. |
| Replay rejects a file | Check for changed bytes or non-increasing timestamps. Start a distinct recording in separate state. |
| A journal pipeline reports `ValueError` | Check saved detector errors, live receiver errors, archive completion and recorded timestamps. See [journal configuration](configuration.md#built-in-inputs). |
| An output fails after a path change | Give the new destination a new output instance name and an empty private directory. |
| Enrichment keeps retrying | Check installed plugin versions, declared dependencies, schema approval, namespace/version, and source availability. |
| A consumer rejects history or generation | Preserve both sides' state and reconcile continuity. Do not skip batches or overwrite cursors. |
| Engine runs but no finding appears | Most reports produce no finding. Inspect health, observation duration, source quality, and reference coverage before assuming an error. |

For a bug report, use a minimal synthetic reproduction and the version details in
[SUPPORT.md](../SUPPORT.md). Private journals and raw archives are not issue
attachments.
