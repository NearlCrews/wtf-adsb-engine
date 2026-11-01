# Private archive analysis

The engine can freeze and replay archive corpora, analyze saved metrics, and
import compact replay into a journal backup. Its commands use explicit paths
and work from any directory after installation. Use Linux and Python 3.14 or newer.
The compact replay importer requires the `archive` extra. From this checkout:

```sh
uv sync --locked --extra archive
source .venv/bin/activate
```

The commands below use that environment's `python`.
The equivalent console commands are `wtf-adsb-backfill-history`,
`wtf-adsb-backfill-metrics` and `wtf-adsb-import-replay`.

Archive data is optional. The [README replay](../README.md) works without an
archive or metadata download. These commands require existing readsb/tar1090-style
history and a compatible engine analysis journal; they cannot import arbitrary
CSV, JSON, or SQLite layouts. Use each command's `--help` for its input options.

Keep artifacts private. The commands create directories with mode 0700 and files
with mode 0600. Outputs must be outside a Git worktree or in an ignored private
directory. The replay importer requires an existing private parent directory and
an existing journal backup. None of these commands should target a running
collector journal. Create a separate copy with SQLite's backup API or `.backup`,
which includes committed WAL contents; copying only a live `.sqlite3` file can
omit them. The importer option `--live-journal` adds a path and hard-link check against a known active
database. It cannot discover every running collector automatically.

## Freeze and replay a corpus

Replace the input paths with your archive, journal backup and aircraft metadata
file. Choose either a specific watchlist cache with `--watchlist` or
`--no-watchlist`; the command does not infer a cache from another project.

```sh
archive_state="$HOME/.local/share/wtf-adsb-engine/archive"
install -d -m 0700 "$archive_state"
python -m wtf_adsb_engine.analysis.backfill_history \
  --output "$archive_state/corpus" freeze \
  --root /path/to/globe_history \
  --journal /path/to/private/journal-backup.sqlite3 \
  --metadata /path/to/aircraft-metadata.csv.gz \
  --no-watchlist
python -m wtf_adsb_engine.analysis.backfill_history \
  --output "$archive_state/corpus" pin-source
python "$archive_state/corpus/run_frozen.py" --workers 4
python -m wtf_adsb_engine.analysis.backfill_history \
  --output "$archive_state/corpus" status
```

Freeze captures compact replay, full traces and advisory records, preserving
input hashes, metadata provenance and an analysis cutoff. `--through YYYY-MM-DD`
requires a completed day; an incomplete ended day needs an explicit recorded
reason. `--snapshots` includes retained snapshots, and repeatable
`--include-prior CORPUS` carries forward hash-checked inputs from older captures.

Pinning copies the analysis code and data into the private corpus. If
`WTF_ADSB_REFERENCE_DIR` is set, its declared reference tables are included in
the private pin and hashes; unrelated files are excluded. The pinned runner
uses those captured tables even if the caller's environment later changes.
These private copies do not change which reference data the engine distributes.
Run a pinned corpus through `run_frozen.py`. For an unpinned corpus, the module's
`run --workers 4` subcommand uses the installed engine. Both resume completed
days and refuse changed code or frozen inputs. `--max-days 1` allows a bounded
first run. `clone-inputs --from CORPUS` starts a separate analysis over the same
frozen inputs. `checkpoint` folds a settled corpus's SQLite WAL.

Results include `days/*.json`, `cross-day.json`, `analysis.json`, receipts and
`corpus.sqlite3`. These are private analysis artifacts, not collection-feed
batches. They retain historical freshness limits and private analysis context;
do not publish them directly.

## Import compact replay into a journal backup

```sh
python -m wtf_adsb_engine.analysis.import_replay \
  --root /path/to/globe_history \
  --database /path/to/private/journal-backup.sqlite3 \
  --metadata /path/to/aircraft-metadata.csv.gz \
  --live-journal /path/to/active/journal.sqlite3
```

This updates the backup in place and records per-file progress and code hashes.
Repeating it skips committed imports. Damaged files remain incomplete and can
be retried. By default, samples from the backup's live collection period are
excluded. `--allow-live-overlap` permits that observation period in the backup,
while continuing to refuse merges into live finding rows. Archive labels count
as saved label states, and compact altitude keeps its unknown reference.
Use the engine's `wtf_adsb.journal` input when projecting this backup into the
validated collection feed.

## Capture and analyze local metrics

```sh
python -m wtf_adsb_engine.analysis.backfill_metrics \
  --output "$archive_state/metrics" freeze \
  --start 2026-06-10T00:00:00+00:00 --end 2026-06-11T00:00:00+00:00 \
  --endpoint http://127.0.0.1:9090/prometheus
python -m wtf_adsb_engine.analysis.backfill_metrics \
  --output "$archive_state/metrics" analyze \
  --corpus "$archive_state/corpus"
```

Capture permits an explicit loopback HTTP endpoint and a completed interval.
Analysis reads only the saved responses and matching frozen corpus metadata.
Sparse metrics do not prove message-level freshness or flight maneuvers;
their findings remain marked ineligible for public export and stories.
