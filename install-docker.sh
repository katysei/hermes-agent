#!/usr/bin/env bash
#
# install-docker.sh — Hermes Agent Docker setup
# =============================================================================
#
# Orchestrator only.  Assumes you're either inside this branch's checkout, or
# the script will clone the patched fork for you (default).  The patches —
# docker-compose.yml, docker/entrypoint.sh, docker/mcp_http_server.py — live
# as real files on the branch; this script just wires them together.
#
# What it does:
#   1. Prereq check (docker, git, openssl, curl)
#   2. If run from outside a Hermes checkout, clones the patched branch
#   3. Generates API_SERVER_KEY in the compose .env (bearer-token auth)
#   4. Ensures ~/.hermes/ exists on the host (bind-mount target)
#   5. docker compose up --build -d  (build image + start gateway + dashboard)
#   6. Polls the gateway health endpoint until 200 OK
#
# Usage:
#       ./install-docker.sh                          # full install (clones if needed)
#       ./install-docker.sh ~/code/hermes            # clone into specific dir
#       ./install-docker.sh --skip-build             # no `--build` flag on compose up
#       ./install-docker.sh --skip-start             # don't `up -d` at the end
#       ./install-docker.sh --dry-run                # show what would happen
#
# Env overrides (bootstrap mode):
#       HERMES_REPO      default: https://github.com/katysei/hermes-agent.git
#       HERMES_BRANCH    default: feat/hermes-install
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

DRY_RUN=0; SKIP_BUILD=0; SKIP_START=0; TARGET=""
for arg in "$@"; do
    case "$arg" in
        --dry-run)    DRY_RUN=1 ;;
        --skip-build) SKIP_BUILD=1 ;;
        --skip-start) SKIP_START=1 ;;
        -h|--help)    sed -n '2,30p' "$0"; exit 0 ;;
        --*)          echo "Unknown flag: $arg" >&2; exit 2 ;;
        *)
            [ -z "$TARGET" ] && TARGET="$arg" || { echo "Unexpected arg: $arg" >&2; exit 2; }
            ;;
    esac
done

say()  { printf "\n\033[1;36m[%s]\033[0m %s\n" "$1" "$2"; }
ok()   { printf "  \033[32m✓\033[0m %s\n" "$1"; }
fail() { printf "  \033[31m✗\033[0m %s\n" "$1" >&2; exit 1; }

# ── 1. Prereqs ─────────────────────────────────────────────────────────────
say "1/5" "Prerequisites"
for cmd in docker git openssl curl; do
    command -v "$cmd" >/dev/null 2>&1 || fail "missing: $cmd"
    ok "$cmd"
done
docker info >/dev/null 2>&1 || fail "docker daemon not reachable"
ok "docker daemon reachable"

# ── 2. Locate or clone checkout ────────────────────────────────────────────
# REMARK: If we're already inside a checkout (patched branch or upstream main
# — doesn't matter, the patched files override either way once present), use
# it.  Otherwise clone our patched fork.  Patched files live at upstream
# paths (docker-compose.yml etc.), so cloning the branch IS the install.
say "2/5" "Checkout"
if [ -f docker-compose.yml ] && [ -f docker/entrypoint.sh ] && [ -d hermes_cli ]; then
    ok "already inside a Hermes checkout: $PWD"
else
    repo="${HERMES_REPO:-https://github.com/katysei/hermes-agent.git}"
    branch="${HERMES_BRANCH:-feat/hermes-install}"
    dest="${TARGET:-hermes-agent}"
    if [ -d "$dest" ]; then
        ok "$dest already exists — using as-is"
    elif [ "$DRY_RUN" = 1 ]; then
        printf "  \033[35m(dry-run)\033[0m would clone $repo ($branch) → $dest\n"
        exit 0
    else
        ok "cloning $repo ($branch) → $dest"
        git clone --branch "$branch" --single-branch --depth 1 "$repo" "$dest"
    fi
    cd "$dest"
fi

# ── 3. API_SERVER_KEY ──────────────────────────────────────────────────────
# REMARK: The compose .env (alongside docker-compose.yml) is auto-loaded by
# docker compose for variable substitution.  This bearer token gates the
# OpenAI-compatible HTTP API on :8642.
say "3/5" "API_SERVER_KEY"
if [ -f .env ] && grep -q "^API_SERVER_KEY=" .env; then
    ok "already in .env, leaving alone"
elif [ "$DRY_RUN" = 1 ]; then
    printf "  \033[35m(dry-run)\033[0m would generate API_SERVER_KEY\n"
else
    printf '\nAPI_SERVER_KEY=%s\n' "$(openssl rand -hex 32)" >> .env
    ok "generated 32-byte hex key into .env"
fi

# ── 4. ~/.hermes/ bind-mount target ────────────────────────────────────────
# REMARK: docker-compose.yml mounts $HOME/.hermes → /opt/data.  Make sure the
# directory exists on the host so Docker doesn't auto-create it as root.
say "4/5" "~/.hermes (bind-mount target)"
if [ -d "$HOME/.hermes" ]; then
    ok "$HOME/.hermes already present"
elif [ "$DRY_RUN" = 1 ]; then
    printf "  \033[35m(dry-run)\033[0m would mkdir -p ~/.hermes\n"
else
    mkdir -p "$HOME/.hermes"
    ok "created empty ~/.hermes (add API keys to ~/.hermes/.env)"
fi

# ── 5. Build + start + verify ──────────────────────────────────────────────
# REMARK: HERMES_UID/GID exported from $(id -u/-g) so files written by the
# container to ~/.hermes stay owned by the host user, not phantom UID 10000.
# --build forces image rebuild; subsequent runs hit BuildKit cache.
say "5/5" "Build + start + verify"
if [ "$DRY_RUN" = 1 ]; then
    printf "  \033[35m(dry-run)\033[0m would run: HERMES_UID=\$(id -u) HERMES_GID=\$(id -g) docker compose up --build -d\n"
    exit 0
fi

if [ "$SKIP_START" = 1 ]; then
    ok "--skip-start set, stopping here"
    exit 0
fi

build_flag=""
[ "$SKIP_BUILD" = 0 ] && build_flag="--build"
HERMES_UID=$(id -u) HERMES_GID=$(id -g) docker compose up $build_flag -d
ok "gateway + dashboard up"

# Poll health endpoints
deadline=$(( $(date +%s) + 60 ))
while [ "$(date +%s)" -lt "$deadline" ]; do
    code=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:8642/v1/health || echo "000")
    [ "$code" = "200" ] && { ok "gateway HTTP: 200 OK"; break; }
    sleep 1
done
deadline=$(( $(date +%s) + 90 ))
while [ "$(date +%s)" -lt "$deadline" ]; do
    code=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:9119/ || echo "000")
    [ "$code" = "200" ] && { ok "dashboard:    200 OK"; break; }
    sleep 2
done

INSTALL_DIR="$(pwd)"
KEY_PFX=$(grep "^API_SERVER_KEY=" .env | head -1 | cut -d= -f2 | head -c 8)
cat <<DONE

═══════════════════════════════════════════════════════════════════════════
  ✓ Hermes Docker installed at: $INSTALL_DIR

  Web dashboard:   http://localhost:9119
  Gateway HTTP:    http://localhost:8642/v1/health  (Bearer auth)
  Bearer token:    starts with ${KEY_PFX}…  (full value in $INSTALL_DIR/.env)

  Add at least one provider API key to:  ~/.hermes/.env
    e.g.  ANTHROPIC_API_KEY=sk-ant-...
          OPENAI_API_KEY=sk-...
          OPENROUTER_API_KEY=sk-or-...
          GEMINI_API_KEY=...
  See INSTALL.md for the full list and where to get each key.

  Talk to the agent:
    docker exec -it -e HERMES_HOME=/opt/data hermes \\
        /opt/hermes/.venv/bin/hermes chat
  Or via HTTP:
    KEY=\$(grep ^API_SERVER_KEY= $INSTALL_DIR/.env | cut -d= -f2)
    curl http://localhost:8642/v1/chat/completions \\
        -H "Authorization: Bearer \$KEY" -H "Content-Type: application/json" \\
        -d '{"model":"hermes-agent","messages":[{"role":"user","content":"hi"}]}'

  Lifecycle:
    docker compose -f $INSTALL_DIR/docker-compose.yml logs -f
    docker compose -f $INSTALL_DIR/docker-compose.yml stop
    docker compose -f $INSTALL_DIR/docker-compose.yml down
═══════════════════════════════════════════════════════════════════════════
DONE
