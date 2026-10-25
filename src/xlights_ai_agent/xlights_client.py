"""Client for xLights' REST automation API and built-in MCP server.

xLights (with the xFade/automation service enabled in Preferences) opens an
HTTP port -- 49913 for instance A, 49914 for instance B -- exposing:

    * ``POST /xlDoAutomation``  the full command dispatcher (see xLights'
      ``documentation/xlDo Commands.txt``)
    * ``POST /mcp``             a stateless Model Context Protocol server with
      convenience tools (addEffect, getModels, renderAll, ...)

Both transports are supported here; REST is the default. The client also
provides connection discovery, ``getModels``, ``addEffect`` (per placement),
clear/replace helpers and plan pushing with progress callbacks.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, List, Optional, Sequence

from .planner import Placement, SequencePlan

__all__ = ["XLightsConnection", "XLightsError", "XLightsClient", "discover",
           "DEFAULT_PORTS"]

DEFAULT_PORTS = (49913, 49914)   # xFade instance A / B


class XLightsError(RuntimeError):
    """Raised when xLights rejects a command or cannot be reached."""

    def __init__(self, message: str, code: Optional[int] = None):
        super().__init__(message)
        self.code = code


@dataclass
class XLightsConnection:
    host: str
    port: int
    transport: str = "rest"       # "rest" | "mcp"

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def __str__(self) -> str:
        return f"{self.host}:{self.port} ({self.transport})"


def _http_json(url: str, payload: Optional[dict] = None,
               timeout: float = 10.0) -> dict:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:300]
        raise XLightsError(f"HTTP {exc.code} from {url}: {detail}", exc.code) from exc
    except urllib.error.URLError as exc:
        raise XLightsError(f"cannot reach xLights at {url}: {exc.reason}") from exc
    except (TimeoutError, OSError) as exc:
        raise XLightsError(f"cannot reach xLights at {url}: {exc}") from exc
    try:
        return json.loads(body) if body.strip() else {}
    except json.JSONDecodeError as exc:
        raise XLightsError(f"non-JSON response from {url}: {body[:200]}") from exc


def discover(host: str = "127.0.0.1", ports: Sequence[int] = DEFAULT_PORTS,
             timeout: float = 1.5,
             transport: str = "rest") -> XLightsConnection:
    """Probe automation ports until one answers ``getVersion``."""
    errors: List[str] = []
    for port in ports:
        conn = XLightsConnection(host=host, port=int(port), transport=transport)
        try:
            client = XLightsClient(conn, timeout=timeout)
            client.get_version()
            return conn
        except XLightsError as exc:
            errors.append(f"{host}:{port} -> {exc}")
    raise XLightsError(
        "No xLights automation port answered on "
        f"{', '.join(str(p) for p in ports)}. Enable the xFade/automation "
        "service in xLights (Preferences -> xFade/xLights Automation), or pass "
        "--host/--port explicitly. Probes: " + "; ".join(errors)
    )


class XLightsClient:
    """Talks to one xLights instance over REST or MCP."""

    def __init__(self, connection: XLightsConnection, timeout: float = 15.0,
                 mcp_id: int = 1):
        self.conn = connection
        self.timeout = timeout
        self._mcp_id = mcp_id

    # -- low-level ---------------------------------------------------------

    def _rest_cmd(self, cmd: str, params: Optional[dict] = None) -> dict:
        payload: Dict[str, object] = {"cmd": cmd}
        if params:
            for k, v in params.items():
                if isinstance(v, (dict, list)):
                    payload[k] = json.dumps(v)   # xLights parses these back with json::parse
                else:
                    payload[k] = v
        out = _http_json(f"{self.conn.base_url}/xlDoAutomation", payload, self.timeout)
        return self._check(out)

    def _mcp_call(self, tool: str, args: dict) -> dict:
        self._mcp_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": self._mcp_id,
            "method": "tools/call",
            "params": {"name": tool, "arguments": args},
        }
        out = _http_json(f"{self.conn.base_url}/mcp", payload, self.timeout)
        if "error" in out:
            raise XLightsError(f"MCP error: {out['error'].get('message')}")
        result = out.get("result") or {}
        text = ""
        for item in result.get("content") or []:
            if isinstance(item, dict) and item.get("type") == "text":
                text = item.get("text", "")
                break
        try:
            parsed = json.loads(text) if text.strip() else {}
        except json.JSONDecodeError:
            parsed = {"res": 200, "msg": text}
        if result.get("isError"):
            parsed.setdefault("res", 500)
        return self._check(parsed)

    @staticmethod
    def _check(out: dict) -> dict:
        if not isinstance(out, dict):
            raise XLightsError(f"unexpected response: {out!r}")
        code = out.get("res")
        if code is not None:
            try:
                code_i = int(code)
            except (TypeError, ValueError):
                code_i = 200
            if code_i >= 400:
                raise XLightsError(str(out.get("msg") or out), code_i)
        return out

    def cmd(self, name: str, **params) -> dict:
        """Run any xLights automation command by name."""
        if self.conn.transport == "mcp":
            return self._mcp_call("xlights_command",
                                  {"cmd": name, "params": params or {}})
        return self._rest_cmd(name, params or None)

    # -- convenience wrappers ---------------------------------------------

    def get_version(self) -> str:
        if self.conn.transport == "mcp":
            out = self._mcp_call("getVersion", {})
        else:
            out = _http_json(f"{self.conn.base_url}/getVersion", None, self.timeout)
        return str(out.get("version") or out.get("msg") or out)

    def get_show_folder(self) -> str:
        out = self.cmd("getShowFolder")
        return str(out.get("folder", ""))

    def get_open_sequence(self) -> dict:
        if self.conn.transport == "mcp":
            out = self._mcp_call("getOpenSequence", {})
        else:
            out = self.cmd("getOpenSequence")
        return out

    def get_models(self, include_groups: bool = True) -> List[str]:
        if self.conn.transport == "mcp":
            out = self._mcp_call("getModels",
                                 {"models": True, "groups": include_groups})
        else:
            out = self.cmd("getModels", models="true",
                           groups="true" if include_groups else "false")
        models = out.get("models")
        if not isinstance(models, list):
            raise XLightsError(f"getModels failed: {out}")
        return [str(m) for m in models]

    def new_sequence(self, media: str, duration_s: Optional[float] = None,
                     frame_ms: int = 50, view: str = "All Models",
                     force: bool = True) -> dict:
        params: Dict[str, object] = {"force": "true" if force else "false",
                                     "frameMS": frame_ms, "view": view}
        if media:
            params["mediaFile"] = media
            params.pop("durationSecs", None)
        else:
            params["durationSecs"] = int(duration_s or 60)
        return self.cmd("newSequence", **params)

    def open_sequence(self, seq: str, force: bool = True) -> dict:
        if self.conn.transport == "mcp":
            return self._mcp_call("openSequence",
                                  {"seq": seq, "force": force, "promptIssues": False})
        return self.cmd("loadSequence", seq=seq, promptIssues="false",
                        force="true" if force else "false")

    def save_sequence(self, seq: Optional[str] = None) -> dict:
        if seq and self.conn.transport == "mcp":
            return self._mcp_call("saveSequence", {"seq": seq})
        if seq:
            return self.cmd("saveSequence", seq=seq)
        if self.conn.transport == "mcp":
            return self._mcp_call("saveSequence", {})
        return self.cmd("saveSequence")

    def render(self, highdef: bool = False) -> dict:
        if self.conn.transport == "mcp":
            return self._mcp_call("renderAll", {"highdef": highdef})
        return self.cmd("renderAll", highdef="true" if highdef else "false")

    def clear_model_effects(self, model: str) -> dict:
        return self.cmd("clearModelEffects", model=model)

    def add_effect(self, placement: Placement) -> dict:
        payload = placement.to_add_effect()
        if self.conn.transport == "mcp":
            # MCP schema wants objects; its flattener stringifies them.
            return self._mcp_call("addEffect", payload)
        return self._rest_cmd("addEffect", payload)

    def status(self) -> dict:
        info: Dict[str, object] = {"connection": str(self.conn)}
        try:
            info["version"] = self.get_version()
        except XLightsError as exc:
            info["version"] = f"unreachable ({exc})"
        try:
            info["show_folder"] = self.get_show_folder()
        except XLightsError:
            info["show_folder"] = ""
        try:
            seq = self.get_open_sequence()
            info["open_sequence"] = seq.get("seq") or ""
            info["media"] = seq.get("media") or ""
            info["duration_ms"] = seq.get("len") or ""
            info["frame_ms"] = seq.get("framems") or ""
        except XLightsError:
            info["open_sequence"] = None
        return info

    # -- plan pushing ------------------------------------------------------

    def push_plan(self, plan: SequencePlan, replace: bool = False,
                  progress: Optional[Callable[[int, int, Placement], None]] = None,
                  pause_s: float = 0.0,
                  models: Optional[Iterable[str]] = None) -> Dict[str, object]:
        """Send every placement to xLights via ``addEffect``.

        With *replace*, the existing unlocked effects of each targeted model
        are cleared first (``clearModelEffects``).
        """
        placements = list(plan.placements)
        if models is not None:
            wanted = set(models)
            placements = [p for p in placements if p.model in wanted]

        cleared: List[str] = []
        if replace:
            for model in dict.fromkeys(p.model for p in placements):
                self.clear_model_effects(model)
                cleared.append(model)

        ok, failed = 0, 0
        last_err: Optional[str] = None
        total = len(placements)
        for i, p in enumerate(placements):
            try:
                self.add_effect(p)
                ok += 1
            except XLightsError as exc:
                failed += 1
                last_err = str(exc)
            if progress is not None:
                progress(i + 1, total, p)
            if pause_s:
                time.sleep(pause_s)
        return {"added": ok, "failed": failed, "cleared": cleared,
                "total": total, "last_error": last_err}
