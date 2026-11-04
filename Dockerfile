FROM ghcr.io/astral-sh/uv:0.13.7@sha256:96f40d3904aad8d0bd010539001d36c8e2188b981e1334471a90c4a1d93bb490 AS uv
FROM python:3.14.9-slim@sha256:35caba83af2fe6da9a5d922f9b0156f21d1246a48278c58a08bd253303ba42e8 AS base

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

FROM base AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy
WORKDIR /build
COPY Dockerfile .dockerignore .python-version pyproject.toml uv.lock README.md LICENSE THIRD_PARTY_NOTICES.md ./
COPY src/ src/
COPY tools/image_inventory.py tools/image_inventory.py
RUN test "$(uname -m)" = x86_64 \
    && uv sync --locked --extra dev --extra archive --no-install-project \
    && uv build --offline --no-build-isolation --wheel \
    && UV_PROJECT_ENVIRONMENT=/opt/venv uv sync --locked --offline --no-dev --extra archive --no-install-project \
    && uv pip install --offline --python /opt/venv/bin/python --no-deps dist/*.whl \
    && uv pip check --python /opt/venv/bin/python \
    && python tools/image_inventory.py --build-root /build --output /metadata/build.json

FROM base AS engine
LABEL org.opencontainers.image.title="WTF ADS-B Engine" \
      org.opencontainers.image.description="Modular ADS-B analysis, JSON evidence, archive tools and private factual viewer" \
      org.opencontainers.image.version="0.1.0" \
      org.opencontainers.image.licenses="MIT"
ENV PATH=/opt/venv/bin:$PATH HOME=/home/engine
COPY --from=build /opt/venv /opt/venv
COPY --from=build /metadata/build.json /usr/local/share/wtf-adsb-engine/build.json
COPY tools/image_inventory.py /usr/local/share/wtf-adsb-engine/image_inventory.py
RUN groupadd --gid 1000 engine \
    && useradd --uid 1000 --gid engine --create-home --home-dir /home/engine --shell /usr/sbin/nologin engine \
    && python /usr/local/share/wtf-adsb-engine/image_inventory.py
WORKDIR /home/engine
USER engine:engine
ENTRYPOINT ["wtf-adsb-engine"]
CMD ["run", "--config", "/config/receiver.toml"]
