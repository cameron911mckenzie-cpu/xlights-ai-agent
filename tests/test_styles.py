"""Style library, prompt suggestion and LLM-merge validation."""

from __future__ import annotations

import pytest

from xlights_ai_agent.styles import (KNOWN_EFFECTS, get_style, list_styles,
                                     merge_llm_suggestion, suggest_style)


def test_library_covers_all_styles():
    styles = list_styles()
    assert len(styles) >= 8
    ids = [s.id for s in styles]
    assert len(ids) == len(set(ids))
    for s in styles:
        assert s.palette and len(s.palette) <= 8
        assert all(c.startswith("#") and len(c) == 7 for c in s.palette)
        assert s.slots, f"{s.id} has no effect slots"
        for slot in s.slots:
            assert slot.effect in KNOWN_EFFECTS, f"{slot.effect} unknown to xLights"
            assert slot.cadence in ("section", "bar", "downbeat", "beat", "half_beat")
            assert slot.role in ("background", "outline", "accent", "detail")


def test_get_style_unknown():
    with pytest.raises(KeyError):
        get_style("does-not-exist")


def test_get_style_is_a_copy():
    s1 = get_style("christmas-classic")
    s1.palette[0] = "#000000"
    s2 = get_style("christmas-classic")
    assert s2.palette[0] != "#000000"


@pytest.mark.parametrize("prompt,expected", [
    ("spooky halloween fun", "spooky-halloween"),
    ("frozen winter wonderland", "winter-wonderland"),
    ("80s synthwave neon drive", "synthwave-retro"),
    ("EDM drop for the club", "electro-drop"),
    ("traditional christmas song", "christmas-classic"),
    ("rainbow pop for kids", "rainbow-pop"),
    ("elegant gold gala", "elegant-gold"),
    ("meteor shower in space", "meteor-storm"),
    ("candy cane fast rush", "candy-cane-rush"),
])
def test_prompt_matches_style(prompt, expected):
    s = suggest_style(prompt)
    assert s.style.id == expected
    assert s.matched_keywords
    assert s.source == "rules"


def test_prompt_fallback_to_all_rounder():
    s = suggest_style("a totally unrelated description")
    assert s.style.id == "classic-chase"


def test_explicit_style_wins_with_tweaks():
    s = suggest_style("green and gold", explicit_style="rainbow-pop")
    assert s.base_id == "rainbow-pop"
    # explicit selection pins the palette: prompt colour words only rewrite
    # palettes for implicit (keyword-matched) suggestions
    assert s.style.palette == get_style("rainbow-pop").palette


def test_color_word_overrides_palette():
    s = suggest_style("purple rain, gothic")
    assert any("palette overridden" in t for t in s.tweaks)
    # every colour comes from the purple family
    purples = {"#A11AFF", "#7500CC", "#C780FF", "#4D0080", "#B84DFF",
               "#8C00E6", "#DFBBFF", "#330066"}
    assert set(s.style.palette) <= purples


def test_mood_tweaks_density():
    s = suggest_style("christmas but make it aggressive with strobes")
    assert s.style.density == "dense"
    s2 = suggest_style("christmas but calm and gentle")
    assert s2.style.density == "sparse"


def test_llm_merge_applies_valid_output():
    base = get_style("classic-chase")
    data = {
        "palette": ["#112233", "AABBCC", {"hexValue": "#445566"}],
        "effects": [
            {"role": "background", "effect": "Bars", "cadence": "section",
             "settings": {"Bars_Cycles": "3"}, "why": "why not"},
            {"role": "outline", "effect": "Explosion9000", "cadence": "beat"},
        ],
        "density": "dense",
        "notes": "made by llm",
    }
    merged = merge_llm_suggestion(base, data)
    assert merged is not None
    style, tweaks = merged
    assert style.palette[0] == "#112233"
    bg = next(s for s in style.slots if s.role == "background")
    assert bg.effect == "Bars" and bg.settings["Bars_Cycles"] == "3"
    # unknown effect name must be rejected -> outline keeps base effect
    outline = next(s for s in style.slots if s.role == "outline")
    assert outline.effect == "Marquee"
    assert style.density == "dense"
    assert style.notes == "made by llm"


def test_llm_merge_rejects_garbage():
    base = get_style("classic-chase")
    assert merge_llm_suggestion(base, None) is None
    assert merge_llm_suggestion(base, {"nonsense": True}) is None
    assert merge_llm_suggestion(base, {"palette": ["zzz"], "effects": []}) is None
