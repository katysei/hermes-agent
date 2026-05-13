# Hermes Agent — slim git-pip image
# =============================================================================
#
# Clones upstream Hermes from git, builds the web dashboard, installs Python
# deps via pip install -e (editable install with hand-picked extras), then
# strips out the build-time dependencies.  Result: ~2-3 GB image (vs ~7.4 GB
# upstream), ~2-3 min cold build (vs ~15 min).
#
# Includes: agent + gateway + dashboard + MCP + messaging bridges + google
# + acp + voice.  Excludes: Playwright, TUI front-end, matrix, modal,
# daytona, vercel, bedrock, etc.
#
# Build:  docker build -t hermes-agent-pip .
# =============================================================================

FROM tianon/gosu:1.19-trixie AS gosu_source
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Permanent runtime deps:
#   tini      — reaps zombies as PID 1
#   git       — for skill loaders that pull from git repos
#   procps    — `ps` for hermes_state.py
#   curl      — health checks
#   ca-certs  — TLS for outbound API calls
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        tini git procps curl ca-certificates && \
    rm -rf /var/lib/apt/lists/*

# Non-root user; UID overridable via HERMES_UID at runtime via the entrypoint.
RUN useradd -u 10000 -m -d /opt/data hermes

# gosu — for the entrypoint's drop-priv step.
COPY --chmod=0755 --from=gosu_source /gosu /usr/local/bin/

# Build + install Hermes from upstream git, then purge build-time deps in
# the SAME RUN so the bytes never become a committed layer.  If purge were
# in a separate RUN, the build-essential/nodejs/npm bytes would persist in
# the previous layer regardless of whether they were "removed" later.
WORKDIR /opt/hermes
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        nodejs npm build-essential gcc python3-dev libffi-dev && \
    # Clone upstream into /opt/hermes (the editable-install location).
    git clone --depth 1 https://github.com/NousResearch/hermes-agent.git . && \
    # Build the dashboard's web_dist (Vite/React → static JS).
    # pyproject.toml's hermes_cli.package_data picks it up so the dashboard
    # works after `pip install -e`.
    (cd web && npm install --prefer-offline --no-audit && npm run build) && \
    # Build the TUI front-end (React+Ink → JS bundle).  Runs at runtime via
    # `hermes --tui`; the Node bundle imports from ui-tui/node_modules so we
    # keep that directory + the `nodejs` runtime in the final image.
    (cd ui-tui && npm install --prefer-offline --no-audit && npm run build) && \
    # Editable install — keeps /opt/hermes as the package's import path so
    # the bundled skills/, web_dist/, and other relative assets all resolve.
    pip install --no-cache-dir -e '.[mcp,web,messaging,google,acp,voice,honcho]' aiohttp && \
    # Drop build-time deps.  Keep nodejs (TUI runtime) + tini, git, procps,
    # curl, ca-certs.  Purge only npm and the C-build toolchain.
    apt-get purge -y npm build-essential gcc python3-dev libffi-dev && \
    apt-get autoremove -y && \
    rm -rf /var/lib/apt/lists/* \
           /opt/hermes/web/node_modules \
           /opt/hermes/.git \
           /tmp/* /var/tmp/*
# Note: /opt/hermes/ui-tui/node_modules is intentionally retained — the TUI
# imports from it at runtime (it's not bundled into a single file).

# Overlay our patched entrypoint + the MCP-over-HTTP transport script.
# These overwrite the upstream copies that came in with the clone above.
COPY --chown=hermes:hermes docker/entrypoint.sh /opt/hermes/docker/entrypoint.sh
COPY --chown=hermes:hermes docker/mcp_http_server.py /opt/hermes/docker/mcp_http_server.py
RUN chmod +x /opt/hermes/docker/entrypoint.sh && \
    chown -R hermes:hermes /opt/hermes

ENV HERMES_HOME=/opt/data
# 8642 — gateway HTTP API ; 9119 — dashboard ; 8000 — MCP-HTTP (opt-in)
EXPOSE 8642 9119 8000
VOLUME [ "/opt/data" ]

ENTRYPOINT [ "/usr/bin/tini", "-g", "--", "/opt/hermes/docker/entrypoint.sh" ]
