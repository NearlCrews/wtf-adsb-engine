"""Explicit, versioned installed-package plugin discovery."""

from importlib.metadata import entry_points

from jsonschema import Draft202012Validator

from .plugin_api.v1 import API_VERSION, KINDS, PluginSpec


def discovered():
    result = []
    for kind in sorted(KINDS):
        for entry in entry_points(group="wtf_adsb_engine." + kind):
            result.append(
                {
                    "id": entry.name,
                    "kind": kind,
                    "distribution": entry.dist.name,
                    "version": entry.dist.version,
                    "object": entry.value,
                }
            )
    return result


def load(kind, name, options):
    if kind not in KINDS:
        raise ValueError("Unknown plugin role")
    matches = [entry for entry in entry_points(group="wtf_adsb_engine." + kind) if entry.name == name]
    if len(matches) != 1:
        raise ValueError(f"Plugin {kind}/{name} is missing or ambiguous")
    factory = matches[0].load()
    spec = getattr(factory, "spec", None)
    if (
        not isinstance(spec, PluginSpec)
        or spec.kind != kind
        or spec.id != name
        or spec.api_version != API_VERSION
    ):
        raise ValueError("Plugin API or identity is incompatible")
    Draft202012Validator.check_schema(spec.config_schema)
    Draft202012Validator(spec.config_schema).validate(options)
    return factory(options)
