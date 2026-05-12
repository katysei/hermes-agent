# Local notes / open TODOs

Personal notes for this checkout. Not committed-worthy upstream — `gitignore` this if it gets in the way.

## 1. `pyproject.toml` — `[mistral]` disabled as a workaround

**Where:** `pyproject.toml` around line 127, inside the `all = [...]` list.

**Why:** The `mistralai` package was yanked from PyPI entirely (the simple-index page exists but lists zero releases). Mistral's current SDK ships as `mistral-common`, with a different API. Until upstream Hermes updates the `[mistral]` extra to depend on `mistral-common`, the `[all]` install can't resolve.

**Current state:** the `"hermes-agent[mistral]"` line in `all` is commented out, with a `# disabled:` note explaining why. Build works; Mistral provider support is the only thing missing.

**To do later:**
- Watch upstream for a `pyproject.toml` PR that migrates to `mistral-common`.
- When that lands: re-enable the line in `all` and revert the comment.
- Or, if you ever actually want Mistral support locally: write a `mistral-common` adapter in `providers/` and re-add the extra ourselves.

## 2. Bundled-skill sync errors on first `gateway run`

**Where:** gateway boot log, three skills under `hermes-home/skills/creative/`:

- `touchdesigner-mcp`
- `baoyu-comic`
- `ascii-video`

**Symptom:** during the "Syncing bundled skills into ~/.hermes/skills/" step at gateway start, these three log `[Errno 21] Is a directory` or `[Errno 17] File exists` and are skipped. Gateway boots fine; the rest of the 89 bundled skills sync cleanly. The skipped skills just aren't refreshed against the bundled versions.

**Why:** the bundled skill ships with `references/` as one shape (file or symlink), the local copy has it as the other (directory or vice versa). The sync code does a non-recursive replace, which fails when shapes disagree.

**To do later (only if you want those three skills working):**
```bash
rm -rf hermes-home/skills/creative/touchdesigner-mcp \
       hermes-home/skills/creative/baoyu-comic \
       hermes-home/skills/creative/ascii-video
HERMES_UID=$(id -u) HERMES_GID=$(id -g) docker compose restart gateway
```
The bundled versions will populate cleanly. No other skills are affected.

## 3. Anthropic credits

Gateway authenticates fine against the Anthropic API. End-to-end Opus calls fail with `Your credit balance is too low` until the account is topped up at https://console.anthropic.com/billing.

Not a code/config issue — flagged here so it doesn't get re-debugged.

## 4. Uncommitted local changes — what's actually local-only

For reference (so we know what's safe to revert vs. what's intentional):

- `Dockerfile.minimal` — slim SDK image, new (uncommitted).
- `docker/mcp_http_server.py` — MCP-over-streamable-HTTP transport, new.
- `docker/entrypoint.sh` — added optional MCP-bridge side-process block.
- `hermes_sdk.py` — Python SDK with five transports (local/HTTP/WS/Docker/MCP).
- `pyproject.toml` — `[mistral]` line in `all` commented out (item #1 above).
- `docker-compose.yml` — diverged from upstream: bind-mount path is `./hermes-home` (not `~/.hermes`), `network_mode: host` replaced with bridge networking + `127.0.0.1` port publishes, dashboard runs with `--host 0.0.0.0 --insecure` (safe because the host-side port is restricted to `127.0.0.1`).
- `hermes-home/` — moved here from `~/.hermes`; gitignored.
- `.gitignore` — added `hermes-home/`.

When pulling upstream, the conflict-prone ones are `docker-compose.yml`, `pyproject.toml`, and `docker/entrypoint.sh`. The others are new files / additive.
