"""Hermes Agent — Python SDK with five transports.

Five ways to talk to Hermes from Python, same object, same methods:

    from hermes_sdk import Hermes

    # 1. In-process (loads AIAgent in this Python process)
    h = Hermes(model="anthropic/claude-sonnet-4.6")

    # 2. Gateway HTTP API (remote, OpenAI-compatible). Default 127.0.0.1:8642.
    h = Hermes.via_http("http://my-vps:8642", token="secret")

    # 3. TUI gateway WebSocket (remote, streaming). Default 127.0.0.1:5900.
    h = Hermes.via_websocket("ws://my-vps:5900/api/ws")

    # 4. Docker — start a Hermes container, talk to it over HTTP (and MCP).
    with Hermes.via_docker(expose_mcp=True) as h:
        h.chat("hello")                              # via HTTP
        h.send_message("telegram:12345", "hi!")      # via MCP

    # 5. MCP over streamable HTTP (talk to bridged messaging surface).
    h = Hermes.via_mcp("http://hermes:8000/mcp")
    h.list_conversations()
    h.send_message("discord:#general", "shipped!")

Method support by transport:

    chat / run_task                                   local, http, ws, docker
    list_crons / create_cron / ...                    local, http, docker
    list_skills / list_toolsets / state / agent       local only
    stop / logs / container                           docker only
    send_message / list_conversations /
    read_messages / list_channels / poll_events /
    list_open_permissions / respond_permission / ...  mcp; or any mode with
                                                      _mcp_url set (e.g.
                                                      via_docker(expose_mcp=True))

Methods that aren't supported in the current mode raise NotImplementedError.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import threading
import time
import uuid
from typing import Any, Callable, Dict, List, Optional


_LOCAL = "local"
_HTTP = "http"
_WS = "ws"
_MCP = "mcp"


def _require(method: str, mode: str, allowed: tuple) -> None:
    if mode not in allowed:
        raise NotImplementedError(
            f"Hermes.{method}() is not available in {mode!r} mode "
            f"(supported: {', '.join(allowed)})."
        )


class Hermes:
    """Wraps Hermes Agent capabilities. Construct directly for in-process,
    or use ``via_http`` / ``via_websocket`` to talk to a remote gateway."""

    # ----- constructors -----

    def __init__(
        self,
        *,
        model: str = "",
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        provider: Optional[str] = None,
        enabled_toolsets: Optional[List[str]] = None,
        disabled_toolsets: Optional[List[str]] = None,
        max_iterations: int = 90,
        system_prompt: Optional[str] = None,
        quiet_mode: bool = True,
        **agent_kwargs: Any,
    ) -> None:
        self._mode = _LOCAL
        self._init_kwargs: Dict[str, Any] = dict(
            model=model,
            api_key=api_key,
            base_url=base_url,
            provider=provider,
            enabled_toolsets=enabled_toolsets,
            disabled_toolsets=disabled_toolsets,
            max_iterations=max_iterations,
            ephemeral_system_prompt=system_prompt,
            quiet_mode=quiet_mode,
        )
        self._init_kwargs.update(agent_kwargs)
        self._agent = None
        self._state = None

    @classmethod
    def via_http(
        cls,
        base_url: str = "http://127.0.0.1:8642",
        *,
        token: Optional[str] = None,
        model: str = "hermes-agent",
        session_id: Optional[str] = None,
        session_key: Optional[str] = None,
        timeout: float = 120.0,
    ) -> "Hermes":
        """Connect to a running Hermes gateway over HTTP.

        Hermes must be running with ``hermes gateway start`` and the
        ``api_server`` platform enabled. ``session_id`` keeps a multi-turn
        conversation; ``session_key`` scopes memory to a logical channel.
        """
        h = cls.__new__(cls)
        h._mode = _HTTP
        h._http_base = base_url.rstrip("/")
        h._http_token = token
        h._http_model = model
        h._http_session_id = session_id
        h._http_session_key = session_key
        h._http_timeout = timeout
        h._http_client = None
        h._docker_client = None
        h._docker_container = None
        h._docker_owned = False
        h._mcp_url = None
        return h

    @classmethod
    def via_websocket(
        cls,
        url: str = "ws://127.0.0.1:5900/api/ws",
        *,
        timeout: float = 300.0,
    ) -> "Hermes":
        """Connect to a running Hermes TUI gateway over WebSocket.

        One ``Hermes.via_websocket(...)`` instance maps to one TUI session.
        The session is created lazily on the first ``chat`` / ``run_task``.
        """
        h = cls.__new__(cls)
        h._mode = _WS
        h._ws_url = url
        h._ws_timeout = timeout
        h._ws_session_id = None
        return h

    @classmethod
    def via_docker(
        cls,
        image: str = "hermes-agent",
        *,
        container_name: Optional[str] = None,
        port: int = 8642,
        host_data_dir: Optional[str] = None,
        api_token: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
        pull: bool = False,
        auto_remove: bool = True,
        network: Optional[str] = None,
        wait_timeout: float = 60.0,
        attach_existing: bool = False,
        expose_mcp: bool = False,
        mcp_port: int = 8000,
        persistent_mcp: bool = False,
    ) -> "Hermes":
        """Start a Hermes container and connect to its HTTP API.

        Requires the ``docker`` Python package (``pip install docker``) and a
        running Docker daemon. Returns a Hermes instance configured for HTTP
        transport, with the container's lifecycle bound to it.

        Prereq: build the image once with ``docker build -t hermes-agent .``
        from the repo root (or pass ``pull=True`` if it's in a registry).

        Args:
            image: Docker image name (default ``hermes-agent``).
            container_name: Container name (auto-generated if None).
            port: Host port to publish the API server on (default 8642).
            host_data_dir: Host directory mounted as ``/opt/data`` (Hermes
                state). When None, the container uses an anonymous volume —
                state is lost on stop with ``auto_remove=True``.
            api_token: Bearer token for the API server (auto-generated if None).
            env: Extra env vars to set in the container.
            pull: ``docker pull`` the image before starting.
            auto_remove: Remove the container when stopped.
            network: ``"host"`` for host networking (Linux only), or None for
                bridge with port publishing.
            wait_timeout: Seconds to wait for the API server to come up.
            attach_existing: Reuse an existing container with this name
                instead of starting a new one.
            expose_mcp: Also start the MCP-over-HTTP bridge inside the
                container and publish it. Enables ``send_message``,
                ``list_conversations``, ``list_channels``, ``poll_events``,
                and the permission methods on the returned instance.
            mcp_port: Host port to publish the MCP bridge on (default 8000).
                Inside the container the bridge always listens on 8000.
            persistent_mcp: Reuse one MCP session across calls (started
                lazily on the first call, torn down on ``close()`` /
                context-manager exit). Off by default — each MCP call
                opens and closes its own session, which is simpler but
                pays the initialize handshake every time.

        Use as a context manager to auto-stop on exit::

            with Hermes.via_docker() as h:
                print(h.chat("hello"))
        """
        try:
            import docker as docker_pkg
        except ImportError as e:
            raise ImportError(
                "Hermes.via_docker() requires the 'docker' package. "
                "Install it with: pip install docker"
            ) from e

        if api_token is None:
            api_token = secrets.token_urlsafe(32)
        if container_name is None:
            container_name = f"hermes-sdk-{uuid.uuid4().hex[:8]}"

        client = docker_pkg.from_env()

        if attach_existing:
            container = client.containers.get(container_name)
            if container.status != "running":
                container.start()
        else:
            if pull:
                client.images.pull(image)
            env_full = {
                "API_SERVER_HOST": "0.0.0.0",
                "API_SERVER_KEY": api_token,
            }
            if expose_mcp:
                env_full["HERMES_MCP_HTTP_PORT"] = "8000"
            if env:
                env_full.update(env)
            volumes: Dict[str, Dict[str, str]] = {}
            if host_data_dir:
                volumes[host_data_dir] = {"bind": "/opt/data", "mode": "rw"}
            run_kwargs: Dict[str, Any] = dict(
                image=image,
                name=container_name,
                detach=True,
                auto_remove=auto_remove,
                environment=env_full,
                volumes=volumes,
                command=["gateway", "run"],
            )
            if network == "host":
                run_kwargs["network_mode"] = "host"
            else:
                ports: Dict[str, int] = {"8642/tcp": port}
                if expose_mcp:
                    ports["8000/tcp"] = mcp_port
                run_kwargs["ports"] = ports
            container = client.containers.run(**run_kwargs)

        h = cls.via_http(f"http://127.0.0.1:{port}", token=api_token)
        h._docker_client = client
        h._docker_container = container
        h._docker_owned = not attach_existing
        if expose_mcp:
            mcp_host_port = 8000 if network == "host" else mcp_port
            h._mcp_url = f"http://127.0.0.1:{mcp_host_port}/mcp"
            h._mcp_persistent = persistent_mcp
        h._wait_for_http(wait_timeout)
        return h

    @classmethod
    def via_mcp(
        cls,
        url: str,
        *,
        headers: Optional[Dict[str, str]] = None,
        timeout: float = 60.0,
        persistent: bool = False,
    ) -> "Hermes":
        """Connect to a Hermes MCP server over streamable HTTP.

        Only the messaging/permission methods (``send_message``,
        ``list_conversations``, ``read_messages``, ``list_channels``,
        ``poll_events``, ``wait_for_event``, ``list_open_permissions``,
        ``respond_permission``) are available — ``chat`` / ``run_task`` /
        cron are not exposed by the MCP surface and raise
        ``NotImplementedError``.

        ``persistent=True`` keeps one MCP session open across calls
        (started lazily on the first call, torn down on ``close()`` /
        context-manager exit). Useful for tight polling loops; otherwise
        each call opens and closes its own session.
        """
        h = cls.__new__(cls)
        h._mode = _MCP
        h._mcp_url = url
        h._mcp_headers = headers or {}
        h._mcp_timeout = timeout
        h._mcp_persistent = persistent
        h._docker_client = None
        h._docker_container = None
        h._docker_owned = False
        return h

    @property
    def mode(self) -> str:
        """Current transport: ``"local"``, ``"http"``, ``"ws"``, or ``"mcp"``.

        Docker-spawned instances report ``"http"`` — Docker is a launch
        mechanism, not a wire protocol.
        """
        return self._mode

    # ----- agent loop -----

    def chat(
        self,
        message: str,
        *,
        stream: Optional[Callable[[str], None]] = None,
    ) -> str:
        """Send a message; return the assistant's final reply text."""
        _require("chat", self._mode, (_LOCAL, _HTTP, _WS))
        if self._mode == _LOCAL:
            return self.agent.chat(message, stream_callback=stream)
        if self._mode == _HTTP:
            data = self._http_chat(message, stream=stream)
            return data["choices"][0]["message"]["content"]
        return self._ws_chat(message, stream=stream)

    def run_task(
        self,
        message: str,
        *,
        system: Optional[str] = None,
        history: Optional[List[Dict[str, Any]]] = None,
        task_id: Optional[str] = None,
        stream: Optional[Callable[[str], None]] = None,
    ) -> Dict[str, Any]:
        """Run the agent loop with tools until completion. Returns the full
        result dict (local: AIAgent.run_conversation; http: ChatCompletion;
        ws: ``{"final_response": str}``)."""
        _require("run_task", self._mode, (_LOCAL, _HTTP, _WS))
        if self._mode == _LOCAL:
            return self.agent.run_conversation(
                message,
                system_message=system,
                conversation_history=history,
                task_id=task_id,
                stream_callback=stream,
            )
        if self._mode == _HTTP:
            return self._http_chat(
                message, system=system, history=history, stream=stream
            )
        return {"final_response": self._ws_chat(message, stream=stream)}

    def reset(self) -> None:
        """Local: discard the underlying agent. HTTP: forget session_id.
        WS: forget the active TUI session."""
        if self._mode == _LOCAL:
            self._agent = None
        elif self._mode == _HTTP:
            self._http_session_id = None
        elif self._mode == _WS:
            self._ws_session_id = None

    @property
    def agent(self):
        """The underlying ``run_agent.AIAgent`` (lazy-instantiated). Local only."""
        _require("agent", self._mode, (_LOCAL,))
        if self._agent is None:
            from run_agent import AIAgent
            self._agent = AIAgent(**self._init_kwargs)
        return self._agent

    # ----- skills (local only) -----

    def list_skills(self) -> Dict[str, Dict[str, Any]]:
        _require("list_skills", self._mode, (_LOCAL,))
        from agent.skill_commands import get_skill_commands
        return get_skill_commands()

    def reload_skills(self) -> Dict[str, Any]:
        _require("reload_skills", self._mode, (_LOCAL,))
        from agent.skill_commands import reload_skills as _reload
        return _reload()

    # ----- toolsets (local only) -----

    def list_toolsets(self) -> Dict[str, Dict[str, Any]]:
        _require("list_toolsets", self._mode, (_LOCAL,))
        from toolsets import get_all_toolsets
        return get_all_toolsets()

    def get_toolset(self, name: str) -> Optional[Dict[str, Any]]:
        _require("get_toolset", self._mode, (_LOCAL,))
        from toolsets import get_toolset as _get
        return _get(name)

    def resolve_toolset(self, name: str) -> List[str]:
        _require("resolve_toolset", self._mode, (_LOCAL,))
        from toolsets import resolve_toolset as _resolve
        return _resolve(name)

    # ----- cron (local + http) -----

    def list_crons(self, include_disabled: bool = False) -> List[Dict[str, Any]]:
        _require("list_crons", self._mode, (_LOCAL, _HTTP))
        if self._mode == _HTTP:
            r = self._http_client_().get(
                f"{self._http_base}/api/jobs",
                params={"include_disabled": str(include_disabled).lower()},
                headers=self._http_headers(),
            )
            r.raise_for_status()
            data = r.json()
            return data.get("jobs", data) if isinstance(data, dict) else data
        from cron.jobs import list_jobs
        return list_jobs(include_disabled=include_disabled)

    def get_cron(self, job_id: str) -> Optional[Dict[str, Any]]:
        _require("get_cron", self._mode, (_LOCAL, _HTTP))
        if self._mode == _HTTP:
            r = self._http_client_().get(
                f"{self._http_base}/api/jobs/{job_id}",
                headers=self._http_headers(),
            )
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.json()
        from cron.jobs import get_job
        return get_job(job_id)

    def create_cron(
        self,
        prompt: Optional[str],
        schedule: str,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Create a scheduled job. See ``cron.jobs.create_job`` for kwargs."""
        _require("create_cron", self._mode, (_LOCAL, _HTTP))
        if self._mode == _HTTP:
            payload = {"prompt": prompt, "schedule": schedule, **kwargs}
            r = self._http_client_().post(
                f"{self._http_base}/api/jobs",
                json=payload,
                headers=self._http_headers(),
            )
            r.raise_for_status()
            return r.json()
        from cron.jobs import create_job
        return create_job(prompt, schedule, **kwargs)

    def update_cron(self, job_id: str, updates: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        _require("update_cron", self._mode, (_LOCAL, _HTTP))
        if self._mode == _HTTP:
            r = self._http_client_().patch(
                f"{self._http_base}/api/jobs/{job_id}",
                json=updates,
                headers=self._http_headers(),
            )
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.json()
        from cron.jobs import update_job
        return update_job(job_id, updates)

    def delete_cron(self, job_id: str) -> bool:
        _require("delete_cron", self._mode, (_LOCAL, _HTTP))
        if self._mode == _HTTP:
            r = self._http_client_().delete(
                f"{self._http_base}/api/jobs/{job_id}",
                headers=self._http_headers(),
            )
            if r.status_code == 404:
                return False
            r.raise_for_status()
            return True
        from cron.jobs import remove_job
        return remove_job(job_id)

    def pause_cron(self, job_id: str, reason: Optional[str] = None) -> Optional[Dict[str, Any]]:
        _require("pause_cron", self._mode, (_LOCAL, _HTTP))
        if self._mode == _HTTP:
            r = self._http_client_().post(
                f"{self._http_base}/api/jobs/{job_id}/pause",
                json={"reason": reason} if reason else {},
                headers=self._http_headers(),
            )
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.json()
        from cron.jobs import pause_job
        return pause_job(job_id, reason=reason)

    def resume_cron(self, job_id: str) -> Optional[Dict[str, Any]]:
        _require("resume_cron", self._mode, (_LOCAL, _HTTP))
        if self._mode == _HTTP:
            r = self._http_client_().post(
                f"{self._http_base}/api/jobs/{job_id}/resume",
                headers=self._http_headers(),
            )
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.json()
        from cron.jobs import resume_job
        return resume_job(job_id)

    def trigger_cron(self, job_id: str) -> Optional[Dict[str, Any]]:
        """Mark a job to run on the next scheduler tick."""
        _require("trigger_cron", self._mode, (_LOCAL, _HTTP))
        if self._mode == _HTTP:
            r = self._http_client_().post(
                f"{self._http_base}/api/jobs/{job_id}/run",
                headers=self._http_headers(),
            )
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.json()
        from cron.jobs import trigger_job
        return trigger_job(job_id)

    # ----- MCP messaging + permissions -----
    #
    # Available in pure MCP mode (``via_mcp(...)``) and in any mode whose
    # instance has ``_mcp_url`` set — most usefully ``via_docker(expose_mcp=True)``,
    # which gives one Hermes object that speaks gateway HTTP for chat/cron
    # and MCP for the messaging bridge.

    def send_message(self, target: str, message: str) -> Dict[str, Any]:
        """Send a message to a platform conversation. ``target`` is
        ``"platform:chat_id"`` (e.g. ``"telegram:6308981865"``)."""
        self._require_mcp("send_message")
        return self._mcp_call("messages_send", {"target": target, "message": message})

    def list_conversations(
        self,
        platform: Optional[str] = None,
        limit: int = 50,
        search: Optional[str] = None,
    ) -> Dict[str, Any]:
        """List active messaging conversations across bridged platforms."""
        self._require_mcp("list_conversations")
        args: Dict[str, Any] = {"limit": limit}
        if platform is not None:
            args["platform"] = platform
        if search is not None:
            args["search"] = search
        return self._mcp_call("conversations_list", args)

    def read_messages(self, session_key: str, limit: int = 50) -> Dict[str, Any]:
        """Read recent messages from a conversation by session key."""
        self._require_mcp("read_messages")
        return self._mcp_call(
            "messages_read", {"session_key": session_key, "limit": limit}
        )

    def list_channels(self, platform: Optional[str] = None) -> Dict[str, Any]:
        """List send-targets across bridged platforms."""
        self._require_mcp("list_channels")
        args: Dict[str, Any] = {}
        if platform is not None:
            args["platform"] = platform
        return self._mcp_call("channels_list", args)

    def poll_events(
        self,
        after_cursor: int = 0,
        session_key: Optional[str] = None,
        limit: int = 20,
    ) -> Dict[str, Any]:
        """Poll for new conversation events since ``after_cursor``."""
        self._require_mcp("poll_events")
        args: Dict[str, Any] = {"after_cursor": after_cursor, "limit": limit}
        if session_key is not None:
            args["session_key"] = session_key
        return self._mcp_call("events_poll", args)

    def wait_for_event(
        self,
        after_cursor: int = 0,
        session_key: Optional[str] = None,
        timeout_ms: int = 30000,
    ) -> Dict[str, Any]:
        """Long-poll for the next conversation event. Returns
        ``{"event": ...}`` on arrival or ``{"event": None, "reason":
        "timeout"}`` when the wait expires. Server caps ``timeout_ms`` at
        300000 (5 min)."""
        self._require_mcp("wait_for_event")
        args: Dict[str, Any] = {
            "after_cursor": after_cursor,
            "timeout_ms": timeout_ms,
        }
        if session_key is not None:
            args["session_key"] = session_key
        # Give the underlying HTTP/SSE call enough headroom to exceed
        # timeout_ms, otherwise the client times out before the server.
        call_timeout = max(self._mcp_call_timeout(), timeout_ms / 1000.0 + 10.0)
        return self._mcp_call("events_wait", args, call_timeout=call_timeout)

    def list_open_permissions(self) -> Dict[str, Any]:
        """List pending approval requests observed by the MCP bridge."""
        self._require_mcp("list_open_permissions")
        return self._mcp_call("permissions_list_open", {})

    def respond_permission(self, id: str, decision: str) -> Dict[str, Any]:
        """Respond to a pending approval. ``decision`` ∈
        ``{"allow-once", "allow-always", "deny"}``."""
        self._require_mcp("respond_permission")
        return self._mcp_call("permissions_respond", {"id": id, "decision": decision})

    # ----- docker container management -----

    @property
    def container(self):
        """The underlying ``docker.models.containers.Container``. Docker mode only."""
        if not getattr(self, "_docker_container", None):
            raise NotImplementedError(
                "Hermes.container is only available in docker mode "
                "(use Hermes.via_docker(...))."
            )
        return self._docker_container

    def stop(self, timeout: int = 10) -> None:
        """Stop the underlying container. Docker mode only.

        If ``auto_remove=True`` was set on ``via_docker`` (the default), the
        container is also removed.
        """
        c = getattr(self, "_docker_container", None)
        if c is None:
            raise NotImplementedError(
                "Hermes.stop() is only available in docker mode."
            )
        try:
            c.stop(timeout=timeout)
        finally:
            if getattr(self, "_http_client", None):
                try:
                    self._http_client.close()
                except Exception:
                    pass
                self._http_client = None

    def logs(self, *, tail: int = 100, follow: bool = False) -> str:
        """Container logs (last ``tail`` lines). Docker mode only."""
        c = getattr(self, "_docker_container", None)
        if c is None:
            raise NotImplementedError(
                "Hermes.logs() is only available in docker mode."
            )
        out = c.logs(tail=tail, stream=follow)
        if follow:
            return out  # generator
        return out.decode("utf-8", errors="replace")

    def close(self) -> None:
        """Release per-instance resources: the persistent MCP session (if
        any) and the pooled HTTP client. Safe to call multiple times.
        Does NOT stop the docker container — use ``stop()`` for that."""
        sess = getattr(self, "_mcp_session_obj", None)
        if sess is not None:
            try:
                sess.close()
            except Exception:
                pass
            self._mcp_session_obj = None
        client = getattr(self, "_http_client", None)
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
            self._http_client = None

    def __enter__(self) -> "Hermes":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            self.close()
        except Exception:
            pass
        if getattr(self, "_docker_owned", False) and getattr(self, "_docker_container", None):
            try:
                self.stop()
            except Exception:
                pass

    def _wait_for_http(self, timeout: float) -> None:
        import httpx
        deadline = time.monotonic() + timeout
        last_err: Optional[Exception] = None
        while time.monotonic() < deadline:
            try:
                r = self._http_client_().get(
                    f"{self._http_base}/v1/health",
                    headers=self._http_headers(),
                    timeout=2.0,
                )
                if r.status_code < 500:
                    return
            except (httpx.HTTPError, OSError) as e:
                last_err = e
            time.sleep(0.5)
        raise TimeoutError(
            f"Hermes API at {self._http_base} did not become ready within "
            f"{timeout}s (last error: {last_err})"
        )

    # ----- state / memory (local only) -----

    @property
    def state(self):
        """Lazy ``hermes_state.HermesState`` — escape hatch for memory/sessions DB."""
        _require("state", self._mode, (_LOCAL,))
        if self._state is None:
            from hermes_state import HermesState
            self._state = HermesState()
        return self._state

    # ----- HTTP transport internals -----

    def _http_client_(self):
        if self._http_client is None:
            import httpx
            self._http_client = httpx.Client(timeout=self._http_timeout)
        return self._http_client

    def _http_headers(self) -> Dict[str, str]:
        h: Dict[str, str] = {}
        if self._http_token:
            h["Authorization"] = f"Bearer {self._http_token}"
        if self._http_session_id:
            h["X-Hermes-Session-Id"] = self._http_session_id
        if self._http_session_key:
            h["X-Hermes-Session-Key"] = self._http_session_key
        return h

    def _http_chat(
        self,
        message: str,
        *,
        system: Optional[str] = None,
        history: Optional[List[Dict[str, Any]]] = None,
        stream: Optional[Callable[[str], None]] = None,
    ) -> Dict[str, Any]:
        messages: List[Dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": system})
        if history:
            messages.extend(history)
        messages.append({"role": "user", "content": message})

        payload: Dict[str, Any] = {
            "model": self._http_model,
            "messages": messages,
            "stream": bool(stream),
        }
        url = f"{self._http_base}/v1/chat/completions"
        headers = self._http_headers()

        if not stream:
            r = self._http_client_().post(url, json=payload, headers=headers)
            r.raise_for_status()
            data = r.json()
            sid = data.get("session_id") or r.headers.get("X-Hermes-Session-Id")
            if sid and not self._http_session_id:
                self._http_session_id = sid
            return data

        return self._http_chat_stream(url, payload, headers, stream)

    def _http_chat_stream(self, url, payload, headers, stream):
        import httpx
        chunks: List[str] = []
        with self._http_client_().stream("POST", url, json=payload, headers=headers) as r:
            r.raise_for_status()
            sid = r.headers.get("X-Hermes-Session-Id")
            if sid and not self._http_session_id:
                self._http_session_id = sid
            for line in r.iter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    obj = json.loads(data)
                except json.JSONDecodeError:
                    continue
                delta = (
                    obj.get("choices", [{}])[0]
                    .get("delta", {})
                    .get("content", "")
                )
                if delta:
                    chunks.append(delta)
                    stream(delta)
        full = "".join(chunks)
        return {
            "choices": [{"message": {"role": "assistant", "content": full}}],
            "streamed": True,
        }

    # ----- WebSocket transport internals -----

    def _ws_chat(
        self,
        message: str,
        *,
        stream: Optional[Callable[[str], None]] = None,
    ) -> str:
        return asyncio.run(self._ws_chat_async(message, stream=stream))

    async def _ws_chat_async(
        self,
        message: str,
        *,
        stream: Optional[Callable[[str], None]] = None,
    ) -> str:
        from websockets.asyncio.client import connect

        async with connect(self._ws_url, max_size=None) as ws:
            if not self._ws_session_id:
                self._ws_session_id = await self._ws_create_session(ws)

            await self._ws_rpc(ws, "prompt.submit", {
                "session_id": self._ws_session_id,
                "text": message,
            })

            chunks: List[str] = []
            final = ""
            while True:
                raw = await asyncio.wait_for(ws.recv(), timeout=self._ws_timeout)
                msg = json.loads(raw)
                if "id" in msg and "method" not in msg:
                    continue
                params = msg.get("params") or {}
                etype = params.get("type") or msg.get("method", "")
                payload = params.get("payload") or {}
                if etype in ("message.delta", "event") and payload:
                    delta = payload.get("text", "")
                    if delta:
                        chunks.append(delta)
                        if stream:
                            stream(delta)
                elif etype == "message.complete":
                    final = payload.get("text") or "".join(chunks)
                    break
                elif etype == "error":
                    raise RuntimeError(payload.get("message", "ws error"))
            return final

    async def _ws_create_session(self, ws) -> str:
        result = await self._ws_rpc(ws, "session.create", {})
        sid = result.get("session_id") if isinstance(result, dict) else None
        if not sid:
            raise RuntimeError(f"session.create returned no session_id: {result!r}")
        return sid

    async def _ws_rpc(self, ws, method: str, params: Dict[str, Any]) -> Any:
        rid = uuid.uuid4().hex[:12]
        await ws.send(json.dumps({
            "jsonrpc": "2.0",
            "id": rid,
            "method": method,
            "params": params,
        }))
        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout=self._ws_timeout)
            msg = json.loads(raw)
            if msg.get("id") == rid:
                if "error" in msg:
                    err = msg["error"]
                    raise RuntimeError(f"{method} failed: {err}")
                return msg.get("result")
            # ignore unrelated events while waiting for the RPC response

    # ----- MCP transport internals -----

    def _require_mcp(self, method: str) -> None:
        if self._mode != _MCP and not getattr(self, "_mcp_url", None):
            raise NotImplementedError(
                f"Hermes.{method}() requires MCP transport "
                f"(use Hermes.via_mcp(...) or "
                f"Hermes.via_docker(expose_mcp=True))."
            )

    def _mcp_call_timeout(self) -> float:
        return float(getattr(self, "_mcp_timeout", 60.0))

    def _mcp_call(
        self,
        tool_name: str,
        args: Dict[str, Any],
        *,
        call_timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        if getattr(self, "_mcp_persistent", False):
            sess = self._ensure_persistent_mcp()
            result = sess.call(tool_name, args, timeout=call_timeout)
            return _parse_mcp_result(tool_name, result)
        # One-shot — spin up a fresh streamable-HTTP session per call.
        return asyncio.run(
            self._mcp_call_oneshot(tool_name, args, call_timeout=call_timeout)
        )

    def _ensure_persistent_mcp(self) -> "_PersistentMCPSession":
        sess = getattr(self, "_mcp_session_obj", None)
        if sess is not None:
            return sess
        sess = _PersistentMCPSession(
            url=self._mcp_url,
            headers=getattr(self, "_mcp_headers", None) or {},
            timeout=self._mcp_call_timeout(),
        )
        sess.start()
        self._mcp_session_obj = sess
        return sess

    async def _mcp_call_oneshot(
        self,
        tool_name: str,
        args: Dict[str, Any],
        *,
        call_timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        try:
            from mcp import ClientSession
            from mcp.client.streamable_http import streamablehttp_client
        except ImportError as e:
            raise ImportError(
                "MCP methods require the 'mcp' package "
                "(install with: pip install mcp)"
            ) from e

        url = getattr(self, "_mcp_url", None)
        if not url:
            raise RuntimeError("MCP URL is not configured on this Hermes instance")

        headers = getattr(self, "_mcp_headers", None) or {}
        timeout = call_timeout if call_timeout is not None else self._mcp_call_timeout()

        _kwargs: Dict[str, Any] = {"timeout": timeout}
        if headers:
            _kwargs["headers"] = headers

        async with streamablehttp_client(url, **_kwargs) as (
            read_stream, write_stream, _get_session_id,
        ):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.call_tool(tool_name, arguments=args)
                return _parse_mcp_result(tool_name, result)


# ---------------------------------------------------------------------------
# MCP transport helpers (module-level)
# ---------------------------------------------------------------------------


def _parse_mcp_result(tool_name: str, result: Any) -> Dict[str, Any]:
    """Turn a ``CallToolResult`` into a plain dict. Raises on ``isError``."""
    text_parts: List[str] = []
    for block in (getattr(result, "content", None) or []):
        if hasattr(block, "text"):
            text_parts.append(block.text)
    text = "\n".join(text_parts)
    if getattr(result, "isError", False):
        raise RuntimeError(
            f"MCP tool {tool_name!r} returned an error: {text or 'unknown'}"
        )
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {"text": text}
    return parsed if isinstance(parsed, dict) else {"result": parsed}


class _PersistentMCPSession:
    """One MCP streamable-HTTP session, kept open on a background event loop.

    Calls into ``call(...)`` are dispatched onto that loop via
    ``run_coroutine_threadsafe``. The session lives until ``close()`` is
    called (or the owning ``Hermes`` is GC'd; the loop runs on a daemon
    thread so it doesn't block process exit either way).
    """

    def __init__(self, *, url: str, headers: Dict[str, str], timeout: float):
        self._url = url
        self._headers = headers
        self._timeout = timeout
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._session: Any = None
        self._ready = threading.Event()
        self._stop: Optional[asyncio.Event] = None
        self._error: Optional[BaseException] = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, name="hermes-mcp-session", daemon=True
        )
        self._thread.start()
        if not self._ready.wait(timeout=self._timeout + 5.0):
            raise TimeoutError(
                f"MCP session at {self._url} did not become ready within "
                f"{self._timeout + 5.0:.1f}s"
            )
        if self._error:
            raise self._error

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._lifecycle())
        except BaseException as e:
            if not self._ready.is_set():
                self._error = e
                self._ready.set()
        finally:
            try:
                loop.close()
            except Exception:
                pass

    async def _lifecycle(self) -> None:
        try:
            from mcp import ClientSession
            from mcp.client.streamable_http import streamablehttp_client
        except ImportError as e:
            self._error = ImportError(
                "MCP persistent session requires the 'mcp' package "
                "(install with: pip install mcp)"
            )
            self._error.__cause__ = e
            self._ready.set()
            return

        self._stop = asyncio.Event()
        kwargs: Dict[str, Any] = {"timeout": self._timeout}
        if self._headers:
            kwargs["headers"] = self._headers
        try:
            async with streamablehttp_client(self._url, **kwargs) as (
                read_stream, write_stream, _get_session_id,
            ):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    self._session = session
                    self._ready.set()
                    await self._stop.wait()
        except BaseException as e:
            if not self._ready.is_set():
                self._error = e
                self._ready.set()

    def call(
        self,
        tool_name: str,
        args: Dict[str, Any],
        *,
        timeout: Optional[float] = None,
    ) -> Any:
        if self._session is None or self._loop is None:
            raise RuntimeError("MCP persistent session is not started")
        coro = self._session.call_tool(tool_name, arguments=args)
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return fut.result(timeout=timeout if timeout is not None else self._timeout + 5.0)

    def close(self) -> None:
        loop = self._loop
        stop = self._stop
        if loop is not None and stop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(stop.set)
            except RuntimeError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        self._session = None
        self._loop = None
        self._stop = None
