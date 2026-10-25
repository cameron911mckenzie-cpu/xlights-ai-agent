"""Local HTTP agent: ``xlights-ai serve``.

A tiny dependency-free server that exposes the agent's abilities over HTTP so
the xLights AI plugin DLL (and any other tool) can use them:

    GET  /                 status page
    GET  /agent/ping       liveness probe used by the plugin's IsAvailable()
    POST /v1/prompt        {prompt}                  -> style suggestion text
    POST /v1/palette       {prompt}                  -> xLights colour palette
    POST /v1/analyze       {audio_path?, beats_path?, meter?} -> beat summary
    POST /v1/plan          {audio_path|beats_path, prompt?, style?, models}
                                                           -> full plan JSON
    POST /v1/sequence      {audio_path|beats_path, prompt?, style?, host?,
                            port?, replace?, render?, save?, dry_run?}
                                                           -> plan + push result

All endpoints accept and return ``application/json`` with permissive CORS so
browser tooling can talk to it too. The server never requires a browser; it is
bound to loopback by default (``--host 0.0.0.0`` to expose on the LAN).
"""

from __future__ import annotations

import json
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Tuple

from . import __version__
from .audio import analyze_beats, decode_audio, load_beats_txt
from .planner import PlanOptions, build_plan
from .sequencer import SequenceRequest, run_pipeline
from .styles import suggest_style
from .xlights_client import XLightsClient, XLightsConnection, XLightsError, discover

__all__ = ["make_server", "serve"]

_DEFAULT_PORT = 8756


class _Handler(BaseHTTPRequestHandler):
    server_version = f"xlights-ai-agent/{__version__}"
    protocol_version = "HTTP/1.1"

    # -- plumbing ----------------------------------------------------------

    def log_message(self, fmt: str, *args) -> None:  # quiet by default
        if getattr(self.server, "verbose", False):
            super().log_message(fmt, *args)

    def _send(self, code: int, obj) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, code: int, message: str) -> None:
        self._send(code, {"error": message})

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON body: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError("JSON body must be an object")
        return data

    # -- HTTP verbs --------------------------------------------------------

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._send(200, {"ok": True})

    def do_GET(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
        if path in ("/", "/index.html"):
            self._send(200, {
                "name": "xlights-ai-agent",
                "version": __version__,
                "service": "AI auto-sequencer agent for xLights",
                "endpoints": [
                    "GET  /agent/ping",
                    "POST /v1/prompt     {prompt}",
                    "POST /v1/palette    {prompt}",
                    "POST /v1/analyze    {audio_path|beats_path, meter?}",
                    "POST /v1/plan       {audio_path|beats_path, prompt?, style?, models}",
                    "POST /v1/sequence   {..., host?, port?, replace?, render?, save?}",
                ],
            })
        elif path == "/agent/ping":
            self._send(200, {"status": "ok", "name": "xlights-ai-agent",
                             "version": __version__})
        else:
            self._error(404, f"unknown path {path}")

    def do_POST(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path.rstrip("/")
        try:
            data = self._read_json()
        except ValueError as exc:
            self._error(400, str(exc))
            return
        try:
            if path == "/v1/prompt":
                self._handle_prompt(data)
            elif path == "/v1/palette":
                self._handle_palette(data)
            elif path == "/v1/analyze":
                self._handle_analyze(data)
            elif path == "/v1/plan":
                self._handle_plan(data)
            elif path == "/v1/sequence":
                self._handle_sequence(data)
            else:
                self._error(404, f"unknown path {path}")
        except (ValueError, KeyError) as exc:
            self._error(400, str(exc))
        except XLightsError as exc:
            self._error(502, f"xLights: {exc}")
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            self._error(500, f"{type(exc).__name__}: {exc}")

    # -- handlers ----------------------------------------------------------

    def _suggest(self, data: dict):
        prompt = str(data.get("prompt") or "")
        style_id = data.get("style") or None
        use_llm = bool(data.get("llm", False))
        return suggest_style(prompt, explicit_style=style_id, use_llm=use_llm)

    def _handle_prompt(self, data: dict) -> None:
        s = self._suggest(data)
        lines = [
            f"Style: {s.style.name} ({s.style.id})",
            s.style.description,
            f"Why: {s.reason}",
            f"Density: {s.style.density}",
            "Palette: " + "  ".join(s.style.palette),
            "Plan:",
        ]
        for slot in s.style.slots:
            secs = ",".join(slot.sections) if slot.sections else "all"
            lines.append(f"  [{slot.role}] {slot.effect} per {slot.cadence} "
                         f"(sections: {secs}, layer {slot.layer}) -- {slot.why}")
        if s.tweaks:
            lines.append("Tweaks: " + "; ".join(s.tweaks))
        self._send(200, {"text": "\n".join(lines),
                         "style": s.to_dict()})

    def _handle_palette(self, data: dict) -> None:
        s = self._suggest(data)
        names = ["Accent 1", "Accent 2", "Accent 3", "Accent 4",
                 "Accent 5", "Accent 6", "Accent 7", "Accent 8"]
        colors = [{"hexValue": hexv, "name": names[i % len(names)],
                   "description": f"{s.style.name} colour {i + 1}"}
                  for i, hexv in enumerate(s.style.palette[:8])]
        self._send(200, {
            "description": f"{s.style.name}: {s.style.description}",
            "colors": colors,
        })

    @staticmethod
    def _analysis_from(data: dict):
        audio_path = data.get("audio_path")
        beats_path = data.get("beats_path")
        meter = int(data.get("meter") or 4)
        bpm_hint = data.get("bpm_hint")
        if beats_path:
            duration = None
            if audio_path:
                try:
                    duration = decode_audio(str(audio_path)).duration_s
                except Exception:  # noqa: BLE001
                    pass
            return load_beats_txt(str(beats_path), meter=meter, duration_s=duration)
        if not audio_path:
            raise ValueError("audio_path or beats_path is required")
        return analyze_beats(decode_audio(str(audio_path)), meter=meter,
                             bpm_hint=float(bpm_hint) if bpm_hint else None)

    def _handle_analyze(self, data: dict) -> None:
        analysis = self._analysis_from(data)
        summary = analysis.summary()
        summary["beats"] = [{"ms": b.time_ms, "bar": b.index_in_bar,
                             "downbeat": b.downbeat, "energy": round(b.energy, 3)}
                            for b in analysis.beats]
        self._send(200, summary)

    def _build_plan(self, data: dict) -> Tuple[object, object, object]:
        analysis = self._analysis_from(data)
        s = self._suggest(data)
        models = data.get("models")
        if not isinstance(models, list) or not models:
            models = None
        if models is None and data.get("client_models"):
            # caller wants us to fetch the model list from xLights
            client = self._client_from(data)
            models = client.get_models(include_groups=bool(data.get("groups", True)))
        if models is None:
            raise ValueError("models is required (array of model/group names) "
                             "unless client_models=true")
        plan = build_plan(analysis, [str(m) for m in models], s.style,
                          options=PlanOptions(
                              frame_ms=int(data.get("frame_ms") or 50),
                              density_scale=float(data.get("density_scale") or 1.0)))
        return analysis, s, plan

    def _handle_plan(self, data: dict) -> None:
        _, s, plan = self._build_plan(data)
        self._send(200, {"plan": plan.to_dict(), "suggestion": s.to_dict()})

    @staticmethod
    def _client_from(data: dict) -> XLightsClient:
        if data.get("host") or data.get("port"):
            conn = XLightsConnection(host=str(data.get("host") or "127.0.0.1"),
                                     port=int(data.get("port") or 49913),
                                     transport=str(data.get("transport") or "rest"))
        else:
            conn = discover(host=str(data.get("discover_host") or "127.0.0.1"))
        return XLightsClient(conn)

    def _handle_sequence(self, data: dict) -> None:
        dry = bool(data.get("dry_run", False))
        req = SequenceRequest(
            audio_path=data.get("audio_path"),
            beats_path=data.get("beats_path"),
            prompt=str(data.get("prompt") or ""),
            style_id=data.get("style"),
            meter=int(data.get("meter") or 4),
            models=[str(m) for m in data["models"]] if isinstance(data.get("models"), list) else None,
            replace=bool(data.get("replace", False)),
            render=bool(data.get("render", False)),
            save=bool(data.get("save", False)),
            save_as=data.get("save_as"),
            new_sequence=data.get("new_sequence"),
            frame_ms=int(data.get("frame_ms") or 50),
            use_llm=bool(data.get("llm", False)),
            dry_run=dry,
        )
        client = None if dry else self._client_from(data)
        result = run_pipeline(req, client=client)
        self._send(200, result.to_dict())


def make_server(host: str = "127.0.0.1", port: int = _DEFAULT_PORT,
                verbose: bool = False) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((host, port), _Handler)
    httpd.verbose = verbose
    httpd.daemon_threads = True
    return httpd


def serve(host: str = "127.0.0.1", port: int = _DEFAULT_PORT,
          verbose: bool = False) -> None:
    httpd = make_server(host, port, verbose)
    print(f"xlights-ai agent listening on http://{host}:{port} "
          f"(POST /v1/prompt, /v1/palette, /v1/analyze, /v1/plan, /v1/sequence)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
    finally:
        httpd.server_close()
