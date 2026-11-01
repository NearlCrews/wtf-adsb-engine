# An independently installed enrichment plugin

This example is a separate Python distribution. Its entry point adds
`org.example.reference_label` to the engine's `enrichers` role without editing
engine code. It imports only `wtf_adsb_engine.plugin_api.v1` and the standard
library.

The example is [MIT licensed](LICENSE), including its separately built wheel.

The plugin adds a configured annotation with explicit source and basis fields.
That annotation is not evidence of an aircraft's identity, owner, mission, or
occupants. The example makes no network requests. Its configuration accepts one
bounded label, and its export schema permits exactly three fields.

From the engine project directory, after installing the
[development dependencies](../../../CONTRIBUTING.md#set-up-a-checkout):

```sh
uv pip install --python .venv/bin/python --no-build-isolation --no-deps ./examples/plugins/reference-label
.venv/bin/wtf-adsb-engine plugins
.venv/bin/wtf-adsb-engine run-once --config examples/plugins/reference-label/example.toml
```

The configuration uses the existing synthetic receiver replay. State goes to
`state/reference-label-demo` and JSON output goes to
`state/reference-label-export`. The input is explicitly synthetic, so production
consumers refuse it by default.

Installing a plugin makes its metadata discoverable. Configuration selects it
for execution and separately approves the schema for its exported namespace.
The engine validates the payload against that local schema and includes its
canonical SHA-256 in batch declarations. A publisher that does not recognize
this extension keeps the observation quarantined.

Plugin code runs with the engine process's permissions. Installation is a trust
decision; typed interfaces and export validation do not isolate hostile Python
code. The example receives a private state directory through `Context` if a
future version needs a cache, but it does not access the engine database.

`tests/test_plugin_sdk.py` builds this distribution with pinned Hatchling and
installs the wheel into a temporary site directory using offline `uv pip`.
Subprocess checks exercise its installed entry point, schema approval, rejected
configuration, evidence revision binding, retry behavior and emitted JSON. No
test installs it into the developer's environment.

## Container image

From the engine checkout, build the base image and run its offline derived-image
check:

```sh
make image
make test-image
```

The check builds this plugin's wheel with the base image's pinned build tools,
installs it into a separate image using a SHA-256 requirement, checks dependency
compatibility, and runs the configured enrichment against synthetic input. It
does not install the plugin in the base image or developer environment.

To retain a derived image for your own configuration, first build the base image
above, then prepare the wheel:

```sh
engine_builder_id=$(docker image inspect --format '{{.Id}}' wtf-adsb-engine:local-build)
engine_plugin_build_id=$(python3 -c 'import uuid; print(uuid.uuid4().hex)')
engine_builder_image="wtf-adsb-engine:plugin-builder-$engine_plugin_build_id"
engine_plugin_wheel_image="wtf-adsb-engine:plugin-wheel-$engine_plugin_build_id"
docker tag "$engine_builder_id" "$engine_builder_image"
docker build --network none --pull=false --build-arg "BUILDER_IMAGE=$engine_builder_image" \
  --file examples/plugins/reference-label/Dockerfile.wheel \
  --tag "$engine_plugin_wheel_image" .
mkdir -p dist/reference-label
engine_plugin_container=$(docker create --entrypoint python "$engine_plugin_wheel_image" --version)
docker cp "$engine_plugin_container:/plugin-dist/." dist/reference-label/
docker rm "$engine_plugin_container"
python3 - <<'PY'
import hashlib
from pathlib import Path
directory = Path("dist/reference-label")
wheel, = directory.glob("*.whl")
digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
(directory / "requirements.txt").write_text(
    f"wtf-adsb-example-reference-label==0.1.0 --hash=sha256:{digest}\n"
)
PY
engine_base_id=$(docker image inspect --format '{{.Id}}' wtf-adsb-engine:local)
engine_base_image="wtf-adsb-engine:plugin-base-$engine_plugin_build_id"
docker tag "$engine_base_id" "$engine_base_image"
docker build --network none --pull=false --build-arg "ENGINE_IMAGE=$engine_base_image" \
  --build-arg "BUILDER_IMAGE=$engine_builder_image" \
  --file examples/plugins/reference-label/Dockerfile \
  --tag wtf-adsb-engine:reference-label dist/reference-label
docker run --rm --network none wtf-adsb-engine:reference-label plugins
docker image rm "$engine_plugin_wheel_image" "$engine_builder_image" "$engine_base_image"
```

The [derived Dockerfile](Dockerfile) installs the exact wheel offline, retains
the `engine` account and refreshes the installed package inventory. Other plugins
need their own reviewed wheels and locked transitive dependencies. Rebuild an
image to change its installed plugins; keep configuration, schema approvals and
persistent state outside the image. A successful plugin installation does not
enable it until a pipeline selects it.

The local source tags above identify the inspected images and work with both
Docker builders. Keep them unchanged while building; BuildKit does not accept
a bare local image ID as a `FROM` reference.
