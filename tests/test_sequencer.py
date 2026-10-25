"""End-to-end pipeline tests (dry run + push against the fake server)."""

from __future__ import annotations

import json
import os


from xlights_ai_agent.sequencer import SequenceRequest, run_pipeline
from xlights_ai_agent.xlights_client import XLightsClient, XLightsConnection


def test_dry_run_writes_plan(song_120, models, tmp_path):
    plan_out = str(tmp_path / "plan.json")
    req = SequenceRequest(
        audio_path=song_120["path"],
        prompt="christmas classic",
        models=models,
        dry_run=True,
        plan_out=plan_out,
    )
    result = run_pipeline(req)
    assert result.push is None
    assert os.path.isfile(plan_out)
    blob = json.load(open(plan_out))
    assert blob["meta"]["style"] == "christmas-classic"
    assert blob["stats"]["placements"] > 20
    assert blob["stats"]["models"] == len(models)
    assert any("dry run" in n for n in result.notes)
    assert result.suggestion.source == "rules"


def test_pipeline_with_beats_file(song_120, models, tmp_path):
    from xlights_ai_agent.audio import analyze_beats, decode_audio, save_beats_txt
    a = analyze_beats(decode_audio(song_120["path"]), meter=4)
    beats_file = str(tmp_path / "s_beats.txt")
    save_beats_txt(a, beats_file)
    req = SequenceRequest(beats_path=beats_file, audio_path=song_120["path"],
                          style_id="electro-drop", models=models, dry_run=True)
    result = run_pipeline(req)
    assert abs(result.analysis.bpm - 120.0) < 4
    assert result.plan.meta["style"] == "electro-drop"


def test_push_via_client(song_120, models, monkeypatch):
    from tests.test_xlights_client import _FakeXLights
    from http.server import HTTPServer
    import threading

    server = HTTPServer(("127.0.0.1", 0), _FakeXLights)
    server.records = []
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        conn = XLightsConnection(host="127.0.0.1", port=server.server_address[1])
        client = XLightsClient(conn)
        req = SequenceRequest(audio_path=song_120["path"],
                              prompt="winter wonderland",
                              replace=True, render=True, save=True,
                              dry_run=False)
        stages = []
        result = run_pipeline(req, client=client,
                              progress=lambda s, i: stages.append(s))
        assert result.push["failed"] == 0
        assert result.push["added"] > 20
        # the fake server's getModels returns exactly these three
        assert set(result.push["cleared"]) == {"All House", "MegaTree",
                                               "RoofOutline"}
        cmds = [c for _, c, _ in server.records]
        assert "clearModelEffects" in cmds
        assert "renderAll" in cmds
        assert "saveSequence" in cmds
        assert {"analyzed", "style", "planned", "pushed"} <= set(stages)
        assert any("rendered" in n for n in result.notes)
        # adds happened after clears
        assert cmds.index("clearModelEffects") < cmds.index("addEffect")
    finally:
        server.shutdown()
        server.server_close()


def test_explicit_model_override_tracked(song_120):
    req = SequenceRequest(audio_path=song_120["path"], style_id="classic-chase",
                          models=["SoloTree"], dry_run=True)
    result = run_pipeline(req)
    assert {p.model for p in result.plan.placements} == {"SoloTree"}
    assert any("explicit model list" in n for n in result.notes)
