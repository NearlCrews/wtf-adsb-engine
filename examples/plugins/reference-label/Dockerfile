ARG ENGINE_IMAGE=wtf-adsb-engine:local
ARG BUILDER_IMAGE=wtf-adsb-engine:local-build
FROM ${BUILDER_IMAGE} AS build
FROM ${ENGINE_IMAGE} AS install
USER root
COPY --from=build /usr/local/bin/uv /tmp/uv
COPY *.whl requirements.txt /plugin-dist/
RUN /tmp/uv pip install --offline --no-index --python /opt/venv/bin/python --no-deps --require-hashes --find-links /plugin-dist -r /plugin-dist/requirements.txt \
    && /tmp/uv pip check --python /opt/venv/bin/python \
    && python /usr/local/share/wtf-adsb-engine/image_inventory.py

FROM ${ENGINE_IMAGE}
COPY --from=install /opt/venv /opt/venv
COPY --from=install /usr/local/share/wtf-adsb-engine/packages.json /usr/local/share/wtf-adsb-engine/packages.json
USER engine:engine
