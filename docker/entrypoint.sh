#!/bin/bash
# Slim entrypoint for the pip-install Hermes Docker image.
#
# Does just three things:
#   1.  Optionally remap the internal `hermes` user's UID/GID to host values
#       (so bind-mounted ~/.hermes files are owned by you, not phantom 10000).
#   2.  Optionally start the MCP-over-streamable-HTTP bridge as a side-process.
#   3.  Drop privileges to `hermes` and exec the foreground command (e.g. `hermes gateway run`).
#
# Differences from the upstream entrypoint.sh: no source-tree path assumptions
# (pip-installed Hermes puts the CLI on PATH directly), no skill-sync nudges
# (the bundled skills are already in /opt/hermes/skills from the Dockerfile's
# sparse-clone step).
set -e

HERMES_HOME="${HERMES_HOME:-/opt/data}"

# ── 1. UID/GID remap ─────────────────────────────────────────────────────────
# If HERMES_UID is set and differs from the baked-in 10000, rewrite the
# /etc/passwd entry before we drop privileges.  This is how files written by
# the container end up owned by the host user on the bind mount.
if [ -n "${HERMES_UID:-}" ] && [ "${HERMES_UID}" != "$(id -u hermes 2>/dev/null || echo 10000)" ]; then
    usermod -u "${HERMES_UID}" hermes 2>/dev/null || true
fi
if [ -n "${HERMES_GID:-}" ] && [ "${HERMES_GID}" != "$(id -g hermes 2>/dev/null || echo 10000)" ]; then
    groupmod -g "${HERMES_GID}" hermes 2>/dev/null || true
fi
# Fix ownership on the home dir + bind mount so the remapped user can write.
chown -R hermes:hermes "${HERMES_HOME}" 2>/dev/null || true
chown -R hermes:hermes /opt/hermes/skills 2>/dev/null || true

# ── 2. Optional MCP-over-HTTP side-process ───────────────────────────────────
# Toggled by HERMES_MCP_HTTP_PORT.  Same backgrounding pattern as upstream:
# the foreground command stays PID-of-interest for the container runtime.
#
#   HERMES_MCP_HTTP_PORT  port (must be set to enable; e.g. 8000)
#   HERMES_MCP_HTTP_HOST  default 0.0.0.0
#   HERMES_MCP_HTTP_PATH  default /mcp
if [ -n "${HERMES_MCP_HTTP_PORT:-}" ]; then
    echo "Starting mcp-http on ${HERMES_MCP_HTTP_HOST:-0.0.0.0}:${HERMES_MCP_HTTP_PORT}${HERMES_MCP_HTTP_PATH:-/mcp} (background)"
    (
        stdbuf -oL -eL gosu hermes python /opt/hermes/docker/mcp_http_server.py 2>&1 \
            | sed -u 's/^/[mcp-http] /'
    ) &
fi

# ── 3. Drop privileges and exec ──────────────────────────────────────────────
# `hermes` is the pip-installed CLI entry point (on PATH after `pip install`).
# Default is `gateway run` (from docker-compose.yml `command:`), but any
# hermes subcommand works.
exec gosu hermes hermes "$@"
