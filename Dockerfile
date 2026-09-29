FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HARVEST_DB=/data/harvest.sqlite
RUN pip install --no-cache-dir uv==0.12.11
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable && \
    groupadd --gid 10001 harvest && useradd --uid 10001 --gid 10001 --no-create-home harvest && \
    mkdir /data && chown harvest:harvest /data
# External OSINT CLIs, e.g. --build-arg HARVEST_TOOL_PACKAGES="maigret==0.6.6". Empty by
# default: HARVEST_TOOLS also defaults to empty, so most deployments must not pay for
# maigret's 28 transitive packages (Flask, lxml, reportlab, XMind, pyvis).
# Installed as isolated uv tools, never into /app/.venv: harvest only ever executes these
# by argv as a subprocess, so their pins must not resolve against the project's own.
# maigret depends on socid-extractor<0.2.0, which would otherwise couple the adapter's
# pinned version to maigret's range.
ARG HARVEST_TOOL_PACKAGES=""
RUN if [ -n "$HARVEST_TOOL_PACKAGES" ]; then \
        for spec in $HARVEST_TOOL_PACKAGES; do \
            UV_TOOL_DIR=/opt/uv-tools UV_TOOL_BIN_DIR=/usr/local/bin \
                uv tool install --no-cache "$spec"; \
        done && \
        chmod -R a+rX /opt/uv-tools /usr/local/bin; \
    fi
# The service user has no home directory and the container filesystem is read-only, so
# HOME must point at writable scratch: maigret creates MAIGRET_HOME on startup and aborts
# with EROFS otherwise. Compose mounts a tmpfs here; its contents are an expendable cache.
ENV PATH="/app/.venv/bin:$PATH" HOME=/tmp
USER 10001:10001
VOLUME ["/data"]
EXPOSE 8000
CMD ["harvest", "serve", "--host", "0.0.0.0"]
