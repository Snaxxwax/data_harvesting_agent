FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HARVEST_DB=/data/harvest.sqlite
RUN pip install --no-cache-dir uv==0.12.11
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable && \
    groupadd --gid 10001 harvest && useradd --uid 10001 --gid 10001 --no-create-home harvest && \
    mkdir /data && chown harvest:harvest /data && \
    mkdir -p /home/harvest/.malfrats/ghunt && chown -R harvest:harvest /home/harvest
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
# ghunt 2.3.4 ships two crashes that make `ghunt email --json` -- the exact invocation in
# tools._ghunt -- fail every time, so the tool could never complete a lookup unpatched.
# 2.3.4 is the newest release on PyPI, so there is no version to bump to. Both are already
# fixed on upstream master but unreleased, hence a build-time patch. Each is behind a grep
# guard so a ghunt that rewrites these lines FAILS the build here rather than shipping a
# binary that dies at run time on the first real target.
#
# (1) parsers/people.py -- `KeyError: 'container'`.
#     Upstream fix: github.com/mxrch/GHunt master, parsers/people.py (the `coverPhoto` loop).
#     Google stopped returning metadata.container for coverPhoto entries; `photo` and
#     `readOnlyProfileInfo` still carry it, which is why only cover photos trip it. The line
#     indexed it unguarded, raising before GHunt wrote any JSON, for any account that has a
#     cover photo set.
#
#     Upstream master uses .get("container", "unknown"), which is deliberately NOT what this
#     applies. tools._GHUNT_PROFILE_FIELDS addresses the **PROFILE** container, so keying
#     cover photos under the literal "unknown" would convert this crash into a silent
#     mismapping: cover_image_url would stop erroring and simply never populate, which is
#     the worse failure because nothing reports it. The same metadata object still carries
#     `containerType`, whose value IS the real container name ("PROFILE"), so preferring it
#     fixes the crash AND keeps the field addressable by the existing mapping. "unknown"
#     remains as a final fallback so behaviour matches upstream when neither key is present.
#
# (2) modules/email.py -- `NameError: name 'photos' is not defined`.
#     Upstream fix: github.com/mxrch/GHunt master, modules/email.py (literal None for both).
#     The --json block references `photos` and `reviews`, but gmaps.get_reviews returns only
#     (err, stats) and neither name is ever assigned in hunt(). This fires for EVERY PROFILE
#     container regardless of the account, so --json never worked in 2.3.4. Upstream's fix is
#     copied verbatim; Harvest reads only the `profile` container, so a null maps.photos is
#     inert for every mapping in tools._GHUNT_PROFILE_FIELDS.
RUN set -e; \
    P=$(find /opt/uv-tools -path "*/ghunt/parsers/people.py" 2>/dev/null | head -1); \
    if [ -n "$P" ]; then \
        grep -q 'self.coverPhotos\[cover_photo_data\["metadata"\]\["container"\]\]' "$P" \
            || { echo "ghunt coverPhoto patch no longer applies to this version; re-check upstream"; exit 1; }; \
        sed -i 's#self\.coverPhotos\[cover_photo_data\["metadata"\]\["container"\]\]#self.coverPhotos[(cover_photo_data.get("metadata") or {}).get("container") or (cover_photo_data.get("metadata") or {}).get("containerType") or "unknown"]#' "$P"; \
        grep -q 'containerType' "$P" || { echo "ghunt coverPhoto patch did not apply"; exit 1; }; \
        python3 -c "import ast,sys; ast.parse(open(sys.argv[1]).read())" "$P"; \
        E=$(find /opt/uv-tools -path "*/ghunt/modules/email.py" | head -1); \
        grep -q '"photos": photos,' "$E" \
            || { echo "ghunt maps-json patch no longer applies to this version; re-check upstream"; exit 1; }; \
        sed -i 's#"photos": photos,#"photos": None,#; s#"reviews": reviews,#"reviews": None,#' "$E"; \
        grep -q '"photos": photos,\|"reviews": reviews,' "$E" \
            && { echo "ghunt maps-json patch did not apply"; exit 1; }; \
        python3 -c "import ast,sys; ast.parse(open(sys.argv[1]).read())" "$E"; \
    fi
# The service user has no home directory and the container filesystem is read-only, so
# HOME must point at writable scratch: maigret creates MAIGRET_HOME on startup and aborts
# with EROFS otherwise. Compose mounts a tmpfs here; its contents are an expendable cache.
#
# /home/harvest exists, owned by the service user, for the opposite case: a tool whose state
# must SURVIVE a restart. GHunt derives $HOME/.malfrats/ghunt/creds.m from Path.home() with
# no override and rewrites it when it refreshes the session, so it needs a writable
# persistent home, not tmpfs. It is empty and unused unless a deployment overrides HOME to
# it and mounts a volume there -- see deploy/ovh-vps/compose.ghunt.yaml. Created here rather
# than by the volume mount so Docker initialises the volume with the right ownership; a
# root-owned fresh volume would be unwritable by the non-root service user.
ENV PATH="/app/.venv/bin:$PATH" HOME=/tmp
USER 10001:10001
VOLUME ["/data"]
EXPOSE 8000
CMD ["harvest", "serve", "--host", "0.0.0.0"]
