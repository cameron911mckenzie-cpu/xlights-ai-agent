"""The local HTTP agent (`xlights-ai serve`) endpoint tests."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from xlights_ai_agent.serve import make_server


@pytest.fixture(scope="module")
def agent(song_120):
    httpd = make_server("127.0.0.1", 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield {"base": base, "song": song_120}
    httpd.shutdown()
    httpd.server_close()


def _get(base, path):
    with urllib.request.urlopen(base + path, timeout=5) as r:
        return json.loads(r.read())


def _post(base, path, payload):
    req = urllib.request.Request(
        base + path, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def test_ping_and_index(agent):
    ping = _get(agent["base"], "/agent/ping")
    assert ping["status"] == "ok"
    idx = _get(agent["base"], "/")
    assert "endpoints" in idx
    with pytest.raises(urllib.error.HTTPError):
        _get(agent["base"], "/nope")


def test_prompt_endpoint(agent):
    out = _post(agent["base"], "/v1/prompt", {"prompt": "spooky halloween"})
    assert "Spooky Halloween" in out["text"]
    assert out["style"]["style"]["id"] == "spooky-halloween"
    assert out["style"]["source"] == "rules"


def test_palette_endpoint(agent):
    out = _post(agent["base"], "/v1/palette", {"prompt": "elegant gold"})
    assert 3 <= len(out["colors"]) <= 8
    for c in out["colors"]:
        assert c["hexValue"].startswith("#")
        assert c["name"]
    assert "gold" in out["description"].lower() or "elegant" in out["description"].lower()


def test_analyze_endpoint(agent):
    out = _post(agent["base"], "/v1/analyze",
                {"audio_path": agent["song"]["path"], "meter": 4})
    assert abs(out["bpm"] - 120.0) < 4
    assert out["beat_count"] > 30
    assert out["beats"][0]["bar"] >= 1
    assert isinstance(out["sections"], list) and out["sections"]


def test_plan_endpoint_requires_models(agent):
    with pytest.raises(urllib.error.HTTPError) as ei:
        _post(agent["base"], "/v1/plan",
              {"audio_path": agent["song"]["path"], "prompt": "christmas"})
    assert ei.value.code == 400
    out = _post(agent["base"], "/v1/plan",
                {"audio_path": agent["song"]["path"], "prompt": "christmas",
                 "models": ["MegaTree", "RoofOutline"]})
    assert out["plan"]["stats"]["placements"] > 5
    assert out["plan"]["meta"]["style"] == "christmas-classic"


def test_sequence_dry_endpoint(agent, tmp_path):
    plan_out = str(tmp_path / "p.json")
    out = _post(agent["base"], "/v1/sequence",
                {"audio_path": agent["song"]["path"], "prompt": "edm drop",
                 "models": ["MegaTree"], "dry_run": True, "plan_out": plan_out})
    assert out["stats"]["placements"] > 0
    assert out["push"] is None


def test_bad_json_is_400(agent):
    import urllib.error
    req = urllib.request.Request(
        agent["base"] + "/v1/prompt", data=b"not json {",
        headers={"Content-Type": "application/json"}, method="POST")
    with pytest.raises(urllib.error.HTTPError) as ei:
        urllib.request.urlopen(req, timeout=5)
    assert ei.value.code == 400
