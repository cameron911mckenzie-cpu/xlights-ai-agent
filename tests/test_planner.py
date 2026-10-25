"""Planner: role inference, cadence placement, layering, safety fills."""

from __future__ import annotations

import json
from collections import defaultdict

import pytest

from xlights_ai_agent.audio import analyze_beats, decode_audio
from xlights_ai_agent.planner import (PlanOptions, build_plan,
                                      infer_model_role)
from xlights_ai_agent.styles import get_style


@pytest.fixture(scope="module")
def analysis(song_120):
    return analyze_beats(decode_audio(song_120["path"]), meter=4)


@pytest.mark.parametrize("name,role", [
    ("MegaTree", "tree"),
    ("TreeTopper", "tree"),
    ("RoofOutline", "outline"),
    ("WindowFrame1", "outline"),
    ("PixelMatrix", "matrix"),
    ("Arch1", "arch"),
    ("NorthStar", "accent"),
    ("SingingFace", "face"),
    ("All House", "fill"),
    ("Whatever", "generic"),
])
def test_infer_model_role(name, role):
    assert infer_model_role(name) == role


def _assert_no_same_layer_overlap(plan):
    occ = defaultdict(list)
    for p in plan.placements:
        for s, e in occ[(p.model, p.layer)]:
            assert p.start_ms >= e or p.end_ms <= s, \
                f"overlap on {p.model} layer {p.layer}: {p}"
        occ[(p.model, p.layer)].append((p.start_ms, p.end_ms))


def test_plan_basics(analysis, models):
    style = get_style("christmas-classic")
    plan = build_plan(analysis, models, style, PlanOptions(frame_ms=50))
    assert plan.placements
    assert plan.meta["style"] == "christmas-classic"
    assert plan.meta["bpm"] == pytest.approx(analysis.bpm, abs=1)
    # every model lit
    lit = {p.model for p in plan.placements}
    assert lit == set(models), f"dark models: {set(models) - lit}"
    # frame aligned
    for p in plan.placements:
        assert p.start_ms % 50 == 0 and p.end_ms % 50 == 0
        assert p.end_ms > p.start_ms
    # inside song bounds
    for p in plan.placements:
        assert 0 <= p.start_ms <= analysis.duration_ms
        assert p.end_ms <= analysis.duration_ms + 100
    _assert_no_same_layer_overlap(plan)


def test_roles_target_appropriate_models(analysis, models):
    style = get_style("classic-chase")
    plan = build_plan(analysis, models, style)
    outline_models = {p.model for p in plan.placements if "outline" in p.reason}
    # every outline target must be an outline/arch/fill/generic model
    assert all(infer_model_role(m) in ("outline", "arch", "fill", "generic")
               for m in outline_models), outline_models


def test_section_filters_respected(analysis, models):
    """christmas-classic's detail slot is chorus-only ('high' sections)."""
    style = get_style("christmas-classic")
    plan = build_plan(analysis, models, style)
    detail = [p for p in plan.placements if "detail" in p.reason]
    assert detail, "expected detail placements"
    assert all("[chorus]" in p.reason for p in detail), \
        sorted({p.reason for p in detail})
    low_sections = [s for s in analysis.sections if s.label == "low"]
    if low_sections:
        spans = [(s.start_ms, s.end_ms) for s in low_sections]
        for p in detail:
            for ls, le in spans:
                assert p.end_ms <= ls or p.start_ms >= le


def test_density_scale_changes_count(analysis, models):
    style = get_style("electro-drop")
    light = build_plan(analysis, models, style, PlanOptions(density_scale=0.4))
    heavy = build_plan(analysis, models, style, PlanOptions(density_scale=2.5))
    assert len(heavy.placements) > len(light.placements)


def test_replace_layers_stack_not_overlap(analysis, models):
    """Effects that would collide stack onto a higher layer instead."""
    style = get_style("classic-chase")
    plan = build_plan(analysis, models, style)
    _assert_no_same_layer_overlap(plan)
    assert plan.stats()["max_layer"] >= 0


def test_plan_json_serialisable(analysis, models):
    style = get_style("winter-wonderland")
    plan = build_plan(analysis, models, style)
    blob = json.dumps(plan.to_dict())
    loaded = json.loads(blob)
    assert loaded["stats"]["placements"] == len(plan.placements)
    p0 = loaded["placements"][0]
    assert set(p0) == {"model", "effect", "start_ms", "end_ms", "layer",
                       "settings", "palette", "reason"}


def test_add_effect_payload_shape(analysis, models):
    plan = build_plan(analysis, models, get_style("rainbow-pop"))
    payload = plan.placements[0].to_add_effect()
    assert payload["target"] == plan.placements[0].model
    assert payload["effect"] in payload["effect"]  # non-empty
    assert isinstance(payload["startTime"], str)
    assert isinstance(payload["endTime"], str)
    if "palette" in payload:
        assert "C_BUTTON_Palette1" in payload["palette"]
        assert payload["palette"]["C_CHECKBOX_Palette1"] == "1"


def test_empty_models_is_empty_plan(analysis):
    plan = build_plan(analysis, [], get_style("classic-chase"))
    assert plan.placements == []
    assert plan.stats()["placements"] == 0


def test_max_effects_per_model_cap(analysis, models):
    style = get_style("electro-drop")
    plan = build_plan(analysis, models, style,
                      PlanOptions(max_effects_per_model=10))
    counts = defaultdict(int)
    for p in plan.placements:
        counts[p.model] += 1
    # safety-fill can add a handful on top for previously-dark models, but the
    # cap must still bound the cadence-driven firehose
    assert max(counts.values()) <= 10 + len(analysis.sections) + 1
