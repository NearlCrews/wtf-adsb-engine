"""Configured plugin pipelines with durable checkpoints and independent sinks."""

import copy
import fcntl
import json
import logging
import re
import threading
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

from jsonschema import Draft202012Validator

from . import registry, wire
from .plugin_api.v1 import Context, DetectionResult, EnrichmentResult, InputBatch, Observation, Retraction
from .storage import Store

INSTANCE = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
LOG = logging.getLogger(__name__)
# What a failure message may carry about a provider: (pattern, replacement) in order.
# URLs go first so that their hosts and paths are not replaced piecemeal.
PROVIDER_DETAIL = (
    (re.compile(r"\b[a-z][a-z0-9+.-]*://\S+", re.I), "<url>"),
    (
        re.compile(
            r"\b(?:api[_-]?key|x-apikey|access[_-]?token|token|secret|password|passwd|authorization)"
            r"\b[\"']?\s*[:=]\s*[\"']?(?:bearer\s+)?[^\s,;'\"]+|\bbearer\s+[^\s,;'\"]+",
            re.I,
        ),
        "<secret>",
    ),
    (re.compile(r"(?:\b[a-z]:\\|~/|(?<![\w.~/])/)[^\s'\",;:]+", re.I), "<path>"),
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b|\[[0-9a-f:]*:[0-9a-f:]*\](?::\d+)?", re.I), "<host>"),
    (
        re.compile(
            r"\b(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}\b(?::\d+)?|\blocalhost\b", re.I
        ),
        "<host>",
    ),
    # Opaque keys: runs of 20 or more letters, digits, underscores, and hyphens with a digit.
    (re.compile(r"\b(?=[a-z_-]*\d)[a-z0-9_-]{20,}\b", re.I), "<secret>"),
)
MESSAGE_LIMIT = 200


def failure_message(error):
    """An exception's class and message, without URLs, paths, keys, or host names.

    The message is cut to MESSAGE_LIMIT characters so that one provider cannot
    flood the log. The patterns are a best effort for operators reading logs; no
    status file or evidence record carries the result.
    """
    # A schema error's own text repeats the whole schema and instance; its message is one line.
    message = getattr(error, "message", None)
    text = " ".join((message if isinstance(message, str) else str(error)).split())
    for pattern, replacement in PROVIDER_DETAIL:
        text = pattern.sub(replacement, text)
    text = wire.PRIVATE_TEXT.sub("<private>", text)
    if len(text) > MESSAGE_LIMIT:
        text = text[: MESSAGE_LIMIT - 3] + "..."
    return type(error).__name__ + (": " + text if text else "")


@contextmanager
def state_lock(directory):
    """Acquire the lock shared by runtime and maintenance commands."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / "engine.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError("Another engine process owns this state directory") from error
        yield


def _binding(value, role):
    wire.keys(value, ("plugin",), ("options", "name"))
    name = value.get("name", value["plugin"].replace(".", "-"))
    if not isinstance(name, str) or not INSTANCE.fullmatch(name):
        raise ValueError("Plugin instance names must be short lowercase identifiers")
    return name, registry.load(role, value["plugin"], value.get("options", {}))


def configuration(path):
    path = Path(path).resolve()
    with path.open("rb") as stream:
        value = tomllib.load(stream)
    wire.keys(
        value,
        ("version", "state_directory", "source_instance", "pipelines", "outputs"),
        ("references", "format", "extensions", "synthetic", "poll_interval_seconds"),
    )
    if type(value["version"]) is not int or value["version"] != 1:
        raise ValueError("Unsupported configuration version")
    if not isinstance(value["source_instance"], str) or not INSTANCE.fullmatch(value["source_instance"]):
        raise ValueError("source_instance must be a persistent lowercase identifier")
    if not isinstance(value.get("synthetic", False), bool):
        raise ValueError("synthetic must be a boolean")
    interval = value.get("poll_interval_seconds", 10)
    if type(interval) not in (float, int) or not 0.1 <= interval <= 3600:
        raise ValueError("Poll interval must be between 0.1 and 3600 seconds")
    state = Path(value["state_directory"])
    value["state_directory"] = str(state if state.is_absolute() else path.parent / state)
    value["_directory"] = path.parent

    def resolve_bindings(node):
        if isinstance(node, dict):
            if str(node.get("plugin", "")).startswith("wtf_adsb."):
                for key in (
                    "path",
                    "directory",
                    "flightaware_key_file",
                    "flightaware_pause_file",
                    "fleet_reference_file",
                ):
                    if key in node.get("options", {}):
                        candidate = Path(node["options"][key])
                        node["options"][key] = str(
                            candidate if candidate.is_absolute() else path.parent / candidate
                        )
            for child in node.values():
                resolve_bindings(child)
        elif isinstance(node, list):
            for child in node:
                resolve_bindings(child)

    resolve_bindings(value)
    return value


def _schemas(config):
    schemas = {}
    for entry in config.get("extensions", []):
        wire.keys(entry, ("namespace", "schema_version", "path"))
        key = (entry["namespace"], entry["schema_version"])
        if (
            key in schemas
            or not re.fullmatch(wire.NAMESPACE, key[0])
            or not re.fullmatch(wire.SEMVER, key[1])
        ):
            raise ValueError("Invalid or duplicate approved extension schema")
        path = config["_directory"] / entry["path"]
        schema = wire.loads(path.read_bytes())
        Draft202012Validator.check_schema(schema)
        if not isinstance(schema, dict) or schema.get("type") != "object":
            raise ValueError("Extension schemas must describe an explicit object")

        # Schemas are local reviewed contracts. Forbid remote resolution and
        # unconstrained object keys throughout the extension schema.
        def check(node):
            if isinstance(node, dict):
                if any(keyword in node for keyword in ("$dynamicRef", "$recursiveRef")):
                    raise ValueError("Dynamic and recursive schema references are unsupported")
                if "patternProperties" in node or "prefixItems" in node:
                    raise ValueError("Extension schemas require fixed fields and homogeneous arrays")
                if "$ref" in node and not node["$ref"].startswith("#/"):
                    raise ValueError("Extension schemas cannot fetch external references")
                types = node.get("type")
                types = [types] if isinstance(types, str) else types or []
                if "object" in types and node.get("additionalProperties") is not False:
                    raise ValueError("Extension objects require an explicit field allowlist")
                for child in node.values():
                    check(child)
            elif isinstance(node, list):
                for child in node:
                    check(child)

        check(schema)

        def constrained(node):
            if not isinstance(node, dict) or not any(
                key in node for key in ("type", "$ref", "enum", "const")
            ):
                raise ValueError("Extension schema fields require explicit constraints")
            for child in node.get("properties", {}).values():
                constrained(child)
            for child in node.get("$defs", {}).values():
                constrained(child)
            if "items" in node:
                constrained(node["items"])
            types = node.get("type")
            if (types == "array" or isinstance(types, list) and "array" in types) and "items" not in node:
                raise ValueError("Extension arrays require an explicit item schema")

        constrained(schema)
        schemas[key] = (Draft202012Validator(schema), wire.digest(schema))
    return schemas


class Runtime:
    def __init__(self, config_path):
        self.config = configuration(config_path)
        self.directory = Path(self.config["state_directory"])
        self.schemas = _schemas(self.config)
        self.references = [_binding(value, "references") for value in self.config.get("references", [])]
        if len({name for name, _ in self.references}) != len(self.references):
            raise ValueError("Duplicate reference instance")
        self.reference_capabilities = {name: name for name, _ in self.references}
        for name, reference in self.references:
            if reference.spec.requires:
                raise ValueError("Reference providers with dependencies are unsupported in SDK v1")
            for capability in reference.spec.provides:
                if (
                    capability in self.reference_capabilities
                    and self.reference_capabilities[capability] != name
                ):
                    raise ValueError("Ambiguous reference capability")
                self.reference_capabilities[capability] = name
        self.pipelines, self.outputs, self.instances = [], [], []
        for pipeline in self.config["pipelines"]:
            wire.keys(pipeline, ("name", "input", "detectors"), ("enrichers",))
            name = pipeline["name"]
            if not isinstance(name, str) or not INSTANCE.fullmatch(name):
                raise ValueError("Invalid pipeline name")
            source = _binding(pipeline["input"], "inputs")
            if getattr(source[1], "mode", "live") not in {"live", "replay"}:
                raise ValueError("Input mode must be live or replay")
            detectors = [_binding(value, "detectors") for value in pipeline["detectors"]]
            enrichers = [_binding(value, "enrichers") for value in pipeline.get("enrichers", [])]
            if not detectors:
                raise ValueError("Every input requires a detector")
            capabilities = set(self.reference_capabilities)
            for _, plugin in [source, *detectors, *enrichers]:
                if set(plugin.spec.requires) - capabilities:
                    raise ValueError("A plugin dependency is missing or ordered after its consumer")
                capabilities.update(plugin.spec.provides)
                if plugin.spec.kind == "detectors":
                    capabilities.add("observations")
            if len({key for key, _ in [source, *detectors, *enrichers]}) != 1 + len(detectors) + len(
                enrichers
            ):
                raise ValueError("Duplicate plugin instance within pipeline")
            self.pipelines.append((name, source, detectors, enrichers))
        if not self.pipelines or len({p[0] for p in self.pipelines}) != len(self.pipelines):
            raise ValueError("Pipelines must have unique names")
        for binding in self.config["outputs"]:
            wire.keys(binding, ("name", "plugin", "options"), ("format",))
            sink = _binding({key: value for key, value in binding.items() if key != "format"}, "outputs")
            formatter = _binding(
                binding.get("format", self.config.get("format", {"plugin": "wtf_adsb.json"})), "formats"
            )
            self.outputs.append((sink[0], sink[1], formatter[1]))
        if not self.outputs or len({o[0] for o in self.outputs}) != len(self.outputs):
            raise ValueError("Outputs must have unique names")
        self.store = None
        self.started = False

    def context(self, name, *, now=None, mode="live", references=None):
        return Context(
            name,
            self.directory / "plugins" / name,
            self.config["source_instance"],
            time.time() if now is None else now,
            references or {},
            mode,
        )

    def start(self):
        if self.started:
            return
        marker = self.directory / "migration-in-progress.json"
        if marker.exists() or marker.is_symlink():
            raise ValueError("Journal migration is incomplete; preserve it and migrate to fresh state")
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory.chmod(0o700)
        self.store = Store(self.directory / "engine.sqlite3", synthetic=self.config.get("synthetic", False))
        self.store.bind_source(self.config["source_instance"])
        bindings = {}
        for pipeline in self.config["pipelines"]:
            name = pipeline["name"]
            bindings["inputs/" + name] = pipeline["input"]
            for detector in pipeline["detectors"]:
                instance = detector.get("name", detector["plugin"].replace(".", "-"))
                bindings["detectors/" + name + "/" + instance] = {"plugin": detector["plugin"]}
        for output in self.config["outputs"]:
            bindings["outputs/" + output["name"]] = {
                **output,
                "format": output.get("format", self.config.get("format", {"plugin": "wtf_adsb.json"})),
            }
        self.store.bind_configuration(bindings)
        self.store.active_inputs([pipeline[0] for pipeline in self.pipelines])
        self.store.register_schemas({key: value[1] for key, value in self.schemas.items()})
        pairs = [("references/" + name, plugin) for name, plugin in self.references]
        for name, source, detectors, enrichers in self.pipelines:
            pairs.extend(
                (name + "/" + instance, plugin) for instance, plugin in [source, *detectors, *enrichers]
            )
        for name, sink, formatter in self.outputs:
            pairs.extend((("outputs/" + name, sink), ("formats/" + name, formatter)))
            self.store.cursor(name)
        for name, plugin in pairs:
            context = self.context(name)
            context.state_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.instances.append(plugin)
            plugin.initialize(context)
        self.started = True

    @contextmanager
    def locked(self):
        with state_lock(self.directory):
            try:
                self.start()
                yield
            finally:
                self.close()

    def close(self):
        errors = []
        for plugin in reversed(self.instances):
            try:
                plugin.close()
            except Exception as error:
                errors.append(type(error).__name__)
        self.instances.clear()
        self.started = False
        if errors:
            raise RuntimeError("One or more plugins failed to close")

    def validate_extension(self, extension):
        key = (extension["namespace"], extension["schema_version"])
        if key not in self.schemas:
            raise ValueError("Extension schema has not been approved for export")
        self.schemas[key][0].validate(extension["payload"])

    def poll(self):
        self.start()
        counts, errors = {}, {}
        references, reference_errors = {}, {}
        for name, plugin in self.references:
            try:
                references[name] = plugin.lookup(self.context("references/" + name))
            except Exception as error:
                reference_errors[name] = type(error).__name__
        for capability, name in self.reference_capabilities.items():
            if name in references:
                references[capability] = references[name]
            elif name in reference_errors:
                reference_errors[capability] = reference_errors[name]
        for name, source, detectors, enrichers in self.pipelines:
            now = time.time()
            mode = getattr(source[1], "mode", "live")
            try:
                required = {
                    capability
                    for _, plugin in [source, *detectors, *enrichers]
                    for capability in plugin.spec.requires
                }
                required.update(
                    plugin.settings["reference"]
                    for _, plugin in enrichers
                    if plugin.spec.id == "wtf_adsb.reference_match"
                )
                if required.intersection(reference_errors):
                    raise ValueError("Required reference is unavailable")
                batch = source[1].poll(
                    self.store.checkpoint(name),
                    self.context(
                        name + "/" + source[0], now=now, mode=mode, references=copy.deepcopy(references)
                    ),
                )
                if not isinstance(batch, InputBatch) or not isinstance(batch.records, tuple):
                    raise ValueError("Input plugin returned an incompatible batch")
                if type(batch.complete) is not bool or mode not in {"live", "replay"}:
                    raise ValueError("Input plugin returned invalid completion or mode metadata")
                analysis_now = batch.watermark if mode == "replay" and batch.watermark is not None else now
                observations, retractions = [], []
                for instance, detector in detectors:
                    context = self.context(
                        name + "/" + instance,
                        now=analysis_now,
                        mode=mode,
                        references=copy.deepcopy(references),
                    )
                    context = Context(
                        context.instance,
                        context.state_directory,
                        self.config["source_instance"] + ":" + name,
                        context.now,
                        context.references,
                        mode,
                    )
                    result = detector.analyze(copy.deepcopy(batch), context)
                    if isinstance(result, DetectionResult):
                        for retirement in result.retractions:
                            if not isinstance(retirement, Retraction):
                                raise ValueError("Detector returned an incompatible retraction")
                            retractions.append(
                                Retraction(
                                    name + ":" + instance + ":" + retirement.key,
                                    retirement.reason_code,
                                    tuple(
                                        name + ":" + instance + ":" + key
                                        for key in retirement.replacement_keys
                                    ),
                                )
                            )
                        result = result.observations
                    # One copy preserves shared daily context without retaining
                    # references to plugin-owned mutable data.
                    for observation in copy.deepcopy(tuple(result)):
                        if not isinstance(observation, Observation):
                            raise ValueError("Detector returned an incompatible observation")
                        data = dict(observation.data)
                        wire.validate_observation(data)
                        for extension in data.get("extensions", []):
                            self.validate_extension(extension)
                        for enrichment_name, enricher in enrichers:
                            fingerprint = wire.digest(data)
                            item = enricher.enrich(
                                copy.deepcopy(data),
                                self.context(
                                    name + "/" + enrichment_name,
                                    now=analysis_now,
                                    mode=mode,
                                    references=copy.deepcopy(references),
                                ),
                            )
                            if not isinstance(item, EnrichmentResult) or item.input_digest != fingerprint:
                                raise ValueError("Enrichment does not match its evidence revision")
                            if item.state != "available":
                                raise ValueError(
                                    "Enrichment unavailable; retain the previous checkpoint for retry"
                                )
                            extension = {
                                "namespace": item.namespace,
                                "schema_version": item.schema_version,
                                "payload": copy.deepcopy(item.payload),
                            }
                            self.validate_extension(extension)
                            data["extensions"] = [*data.get("extensions", []), extension]
                            wire.validate_observation(data)
                        observations.append(Observation(name + ":" + instance + ":" + observation.key, data))
                health = {
                    "ready": batch.watermark is not None,
                    "complete": bool(batch.complete),
                    "mode": mode,
                    "last_receiver_poll": batch.watermark if mode == "live" else None,
                    "analysis_watermark": batch.watermark,
                }
                counts[name] = self.store.commit(
                    name,
                    batch.checkpoint,
                    observations,
                    health,
                    timestamp=now,
                    retractions=tuple(retractions),
                )
            except Exception as error:
                LOG.warning("Pass %s failed (%s)", name, failure_message(error))
                self.store.input_failed(name, now, mode=mode)
                errors[name] = type(error).__name__
        return {"changes": counts, "errors": errors, "references": reference_errors}

    def deliver(self, binding):
        name, sink, formatter = binding
        try:
            cursor = self.store.cursor(name)
            context = self.context("outputs/" + name)
            format_context = self.context("formats/" + name)
            while pending := self.store.next_envelope(cursor):
                # The store's batch is a fresh object, so the formatter may keep or change it;
                # the saved canonical envelope is what its artifact must still encode.
                batch, expected = pending
                start, end = batch["sequence_start"], batch["sequence_end"]
                artifact = self.store.artifact(name, start)
                if artifact is None:
                    artifact = formatter.encode(batch, format_context)
                    # A canonical artifact (the JSON format's) needs no second parse to compare.
                    if not isinstance(artifact, bytes) or (
                        artifact != expected + b"\n" and wire.canonical(wire.loads(artifact)) != expected
                    ):
                        raise ValueError("Output format changed the canonical evidence envelope")
                    self.store.save_artifact(name, start, artifact)
                sink.deliver(artifact, self.manifest(), context)
                cursor = end
                self.store.acknowledged(name, cursor)
            sink.deliver(b"", self.manifest(), context)
            self.store.acknowledged(name, cursor)
        except Exception as error:
            LOG.warning("Output %s failed (%s)", name, failure_message(error))
            try:
                self.store.failed(name, error)
            except Exception as status_error:
                LOG.warning("Cannot save output %s failure (%s)", name, type(status_error).__name__)
            return type(error).__name__
        return None

    def manifest(self):
        state = self.store.state()
        return {
            "schema": "wtf-adsb.manifest",
            "schema_version": wire.VERSION,
            "profile": wire.PROFILE,
            "stream_id": state["stream_id"],
            "generation": state["generation"],
            "head_sequence": state["head_sequence"],
            "first_sequence": 1,
            "updated_at": wire.utc(),
            "health": state["health"],
            "batches": [],
        }

    def once(self):
        result = self.poll()
        with ThreadPoolExecutor(max_workers=len(self.outputs)) as pool:
            errors = list(pool.map(self.deliver, self.outputs))
        result["outputs"] = {
            binding[0]: error or "ready" for binding, error in zip(self.outputs, errors, strict=True)
        }
        return result

    def run(self, stop=None):
        from .maintenance import write_json

        stop = stop or threading.Event()
        interval = self.config.get("poll_interval_seconds", 10)
        self.start()

        def worker(binding):
            while not stop.is_set():
                self.deliver(binding)
                stop.wait(min(interval, 5))

        workers = [
            threading.Thread(target=worker, args=(binding,), name="output-" + binding[0], daemon=True)
            for binding in self.outputs
        ]
        for thread in workers:
            thread.start()
        progress = {"last_poll_started": time.time(), "last_poll_completed": time.time()}

        def heartbeat():
            while not stop.is_set():
                statuses = [source[1].health() for _, source, _, _ in self.pipelines]
                try:
                    write_json(
                        self.directory / "heartbeat.json",
                        {
                            "updated_at": time.time(),
                            "inputs_alive": all(status.get("alive", True) for status in statuses),
                            "background_collection": all("alive" in status for status in statuses),
                            **progress,
                        },
                    )
                except OSError:
                    LOG.warning("Cannot save process liveness")
                stop.wait(5)

        monitor = threading.Thread(target=heartbeat, name="liveness", daemon=True)
        monitor.start()
        try:
            while not stop.is_set():
                progress["last_poll_started"] = time.time()
                print(json.dumps(self.poll()), flush=True)
                progress["last_poll_completed"] = time.time()
                stop.wait(interval)
        finally:
            stop.set()
            monitor.join(timeout=10)
            for thread in workers:
                thread.join(timeout=65)
