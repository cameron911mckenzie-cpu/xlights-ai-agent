"""xLights client tests against a fake automation/MCP server."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from xlights_ai_agent.planner import Placement
from xlights_ai_agent.xlights_client import (XLightsClient, XLightsConnection,
                                             XLightsError, discover)


class _FakeXLights(BaseHTTPRequestHandler):
    """Records every command it receives; canned responses per command."""
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    # -- helpers -----------------------------------------------------------

    def _body_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        return json.loads(raw) if raw.strip() else {}

    def _send(self, obj, code=200):
        blob = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)

    @property
    def rec(self):
        return self.server.records

    # -- REST --------------------------------------------------------------

    def do_GET(self):
        if self.path == "/getVersion":
            self._send({"version": "2099.9-fake"})
        else:
            self._send({"res": 404, "msg": "no such endpoint"}, 404)

    def do_POST(self):
        if self.path == "/xlDoAutomation":
            payload = self._body_json()
            cmd = payload.get("cmd", "?")
            self.rec.append(("rest", cmd, payload))
            if cmd == "getModels":
                self._send({"res": 200,
                            "models": ["All House", "MegaTree", "RoofOutline"]})
            elif cmd == "addEffect":
                # echo back so tests can assert what arrived
                self._send({"res": 200, "worked": "true", "got": payload})
            elif cmd in ("clearModelEffects", "renderAll", "saveSequence",
                         "newSequence", "loadSequence", "getOpenSequence",
                         "getShowFolder"):
                self._send({"res": 200, "msg": f"{cmd} ok",
                            "seq": "Show.xsq", "len": "24000",
                            "framems": "50", "folder": "/show"})
            else:
                self._send({"res": 504, "msg": f"unknown command {cmd}"})
        elif self.path == "/mcp":
            payload = self._body_json()
            params = payload.get("params", {})
            tool = params.get("name", "?")
            args = params.get("arguments", {})
            self.rec.append(("mcp", tool, args))
            if tool == "xlights_command":
                cmd = args.get("cmd", "?")
                if cmd == "getModels":
                    body = {"res": 200, "models": ["MegaTree"]}
                else:
                    body = {"res": 200, "msg": f"{cmd} ok"}
            elif tool == "getVersion":
                body = {"res": 200, "version": "2099.9-mcp"}
            elif tool in ("getModels", "addEffect", "deleteEffect",
                          "saveSequence", "renderAll", "openSequence"):
                if tool == "getModels":
                    body = {"res": 200, "models": ["MegaTree"]}
                else:
                    body = {"res": 200, "msg": f"{tool} ok", "worked": "true"}
            else:
                body = {"res": 504, "msg": f"unknown tool {tool}"}
            resp = {
                "jsonrpc": "2.0",
                "id": payload.get("id"),
                "result": {"content": [{"type": "text",
                                        "text": json.dumps(body)}],
                           "isError": False},
            }
            self._send(resp)
        else:
            self._send({"res": 404, "msg": "nope"}, 404)


@pytest.fixture()
def fake_server():
    server = HTTPServer(("127.0.0.1", 0), _FakeXLights)
    server.records = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture()
def client(fake_server):
    conn = XLightsConnection(host="127.0.0.1", port=fake_server.server_address[1],
                             transport="rest")
    return XLightsClient(conn)


def test_discover(fake_server):
    port = fake_server.server_address[1]
    conn = discover(host="127.0.0.1", ports=(port, 1))
    assert conn.port == port
    assert "2099" in XLightsClient(conn).get_version()


def test_discover_failure():
    with pytest.raises(XLightsError) as ei:
        discover(host="127.0.0.1", ports=(1,), timeout=0.4)
    assert "No xLights automation port" in str(ei.value)


def test_get_models(client, fake_server):
    models = client.get_models()
    assert models == ["All House", "MegaTree", "RoofOutline"]
    kind, cmd, payload = fake_server.records[-1]
    assert kind == "rest" and cmd == "getModels"
    assert payload["groups"] == "true"


def test_add_effect_rest_stringifies_objects(client, fake_server):
    p = Placement(model="MegaTree", effect="On", start_ms=500, end_ms=1000,
                  layer=1, settings={"Eff_On_Start": "5"},
                  palette={"C_BUTTON_Palette1": "#FF0000",
                           "C_CHECKBOX_Palette1": "1"})
    out = client.add_effect(p)
    assert out["res"] == 200
    kind, cmd, payload = fake_server.records[-1]
    assert cmd == "addEffect"
    # settings/palette must arrive as JSON strings (xLights json::parse's them)
    assert isinstance(payload["settings"], str)
    assert json.loads(payload["settings"])["Eff_On_Start"] == "5"
    assert isinstance(payload["palette"], str)
    assert payload["startTime"] == "500"
    assert payload["layer"] == "1"
    assert payload["target"] == "MegaTree"


def test_push_plan_with_replace_orders_clear_first(client, fake_server):
    from xlights_ai_agent.planner import SequencePlan
    placements = [
        Placement(model="MegaTree", effect="On", start_ms=0, end_ms=50),
        Placement(model="MegaTree", effect="On", start_ms=50, end_ms=100),
        Placement(model="RoofOutline", effect="On", start_ms=0, end_ms=50),
    ]
    plan = SequencePlan(meta={}, placements=placements)
    seen = []
    out = client.push_plan(plan, replace=True,
                           progress=lambda i, t, p: seen.append((i, t)))
    assert out["added"] == 3 and out["failed"] == 0
    assert set(out["cleared"]) == {"MegaTree", "RoofOutline"}
    cmds = [c for _, c, _ in fake_server.records]
    # both clears happen before any addEffect
    first_add = cmds.index("addEffect")
    assert cmds.index("clearModelEffects") < first_add
    assert cmds.count("clearModelEffects") == 2
    assert seen[-1] == (3, 3)


def test_error_surfaces_message(client, fake_server):
    with pytest.raises(XLightsError) as ei:
        client.cmd("someBogusCommand")
    assert "unknown command" in str(ei.value)


def test_mcp_transport(fake_server):
    conn = XLightsConnection(host="127.0.0.1", port=fake_server.server_address[1],
                             transport="mcp")
    c = XLightsClient(conn)
    assert "mcp" in c.get_version()
    assert c.get_models() == ["MegaTree"]
    p = Placement(model="MegaTree", effect="Bars", start_ms=0, end_ms=50)
    c.add_effect(p)
    kinds = [k for k, _, _ in fake_server.records]
    assert all(k == "mcp" for k in kinds)
    tool, args = fake_server.records[-1][1], fake_server.records[-1][2]
    assert tool == "addEffect"
    assert args["effect"] == "Bars"       # objects stay objects over MCP
    assert isinstance(args["settings"] if "settings" in args else {}, dict)


def test_status(client):
    s = client.status()
    assert s["version"] == "2099.9-fake"
    assert s["open_sequence"] == "Show.xsq"
    assert s["frame_ms"] == "50"
