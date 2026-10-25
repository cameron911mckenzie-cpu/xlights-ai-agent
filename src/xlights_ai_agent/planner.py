"""Turn a beat analysis, a model list and a style into concrete placements.

The planner walks every :class:`~xlights_ai_agent.styles.EffectSlot` of the
style and stamps it across the song according to its cadence:

    section    -> one effect spanning a whole musical section
    bar        -> one effect per bar (``meter`` beats)
    downbeat   -> one effect per bar start
    beat       -> one effect per beat
    half_beat  -> two effects per beat (dense styles)

Model names are classified into roles (tree/matrix -> background fill,
outline/arch/strip -> outline, star/sign -> accent, ...), unmatched models
get a light background treatment so nothing in the layout stays dark, and
collisions on the same model+layer stack onto the next layer instead of
overlapping. Times snap to the sequence frame grid (default 50 ms).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from .audio import BeatAnalysis, Section
from .styles import (ROLE_ACCENT, ROLE_BACKGROUND, ROLE_DETAIL, ROLE_OUTLINE,
                     Style)

__all__ = ["Placement", "SequencePlan", "infer_model_role", "build_plan"]


@dataclass
class Placement:
    """One effect on one model over a time range (what ``addEffect`` needs)."""

    model: str
    effect: str
    start_ms: int
    end_ms: int
    layer: int = 0
    settings: Dict[str, str] = field(default_factory=dict)
    palette: Dict[str, str] = field(default_factory=dict)
    reason: str = ""

    def to_add_effect(self) -> dict:
        payload = {
            "target": self.model,
            "effect": self.effect,
            "startTime": str(self.start_ms),
            "endTime": str(self.end_ms),
            "layer": str(self.layer),
        }
        if self.settings:
            payload["settings"] = dict(self.settings)
        if self.palette:
            payload["palette"] = dict(self.palette)
        return payload

    def to_dict(self) -> dict:
        return {
            "model": self.model,
            "effect": self.effect,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "layer": self.layer,
            "settings": dict(self.settings),
            "palette": dict(self.palette),
            "reason": self.reason,
        }


@dataclass
class SequencePlan:
    """A full auto-sequence: metadata plus every effect placement."""

    meta: dict
    placements: List[Placement]

    @property
    def model_count(self) -> int:
        return len({p.model for p in self.placements})

    def stats(self) -> dict:
        by_effect: Dict[str, int] = {}
        by_model: Dict[str, int] = {}
        max_layer = 0
        for p in self.placements:
            by_effect[p.effect] = by_effect.get(p.effect, 0) + 1
            by_model[p.model] = by_model.get(p.model, 0) + 1
            max_layer = max(max_layer, p.layer)
        return {
            "placements": len(self.placements),
            "models": len(by_model),
            "effects": len(by_effect),
            "max_layer": max_layer,
            "by_effect": dict(sorted(by_effect.items(), key=lambda kv: -kv[1])),
        }

    def to_dict(self) -> dict:
        return {
            "meta": dict(self.meta),
            "stats": self.stats(),
            "placements": [p.to_dict() for p in self.placements],
        }


# ---------------------------------------------------------------------------
# Model classification
# ---------------------------------------------------------------------------

_ROLE_PATTERNS: List[Tuple[str, Tuple[str, ...]]] = [
    # order matters: first match wins, so specific roles come first
    ("arch", ("arch",)),
    ("tree", ("tree", "mega", "pole", "totem", "pylon", "spiral tower")),
    ("matrix", ("matrix", "screen", "grid", "door", "window panel")),
    ("face", ("face", "singing", "mouth", "lyric", "spiral face")),
    ("outline", ("outline", "roof", "frame", "gutter", "perimeter", "eaves",
                 "strip", "string", "line", "corner", "ledge", "fence",
                 "railing", "walkway", "path", "border", "trim")),
    ("accent", ("star", "snowflake", "sign", "sled", "gift", "present",
                "wreath", "candy", "heart", "cross", "arrow", "column",
                "present", "ring", "wheel", "spinner", "propeller")),
    ("fill", ("all ", "group", "yard", "house", "display", "show", "front",
              "scene", "village", "town")),
]


def infer_model_role(name: str) -> str:
    """Guess what a model *is* from its name -> one of the role names above.

    Returns one of: ``tree``, ``matrix``, ``arch``, ``outline``, ``face``,
    ``accent``, ``fill``, ``generic``.
    """
    low = " " + name.lower().strip() + " "
    for role, words in _ROLE_PATTERNS:
        for w in words:
            if w in low:
                return role
    return "generic"


# Role -> slot roles that may target it (fallback chain handled in build_plan).
_TARGETS: Dict[str, Tuple[str, ...]] = {
    ROLE_BACKGROUND: ("tree", "matrix", "fill", "generic", "arch", "face"),
    ROLE_OUTLINE: ("outline", "arch", "fill", "generic"),
    ROLE_ACCENT: ("accent", "star", "face", "fill", "generic"),
    ROLE_DETAIL: ("tree", "matrix", "fill", "generic", "accent",
                  "outline", "arch", "face"),
}


def _palette_dict(style: Style, slots_idx: Sequence[int]) -> Dict[str, str]:
    """Map a slot's colour picks onto xLights' 8 palette buttons."""
    out: Dict[str, str] = {}
    for btn, idx in enumerate(slots_idx, start=1):
        color = style.palette[idx % len(style.palette)] if style.palette else "#FFFFFF"
        out[f"C_BUTTON_Palette{btn}"] = color
        out[f"C_CHECKBOX_Palette{btn}"] = "1"
    return out


# ---------------------------------------------------------------------------
# Cadence windows
# ---------------------------------------------------------------------------

def _cadence_windows(analysis: BeatAnalysis, cadence: str,
                     section: Section) -> List[Tuple[int, int]]:
    """List of (start_ms, end_ms) windows for *cadence* inside *section*."""
    beats = [b for b in analysis.beats
             if section.start_s - 1e-6 <= b.time_s < section.end_s - 1e-6]
    if not beats:
        return []
    meter = max(2, analysis.meter)
    out: List[Tuple[int, int]] = []
    sec_end = section.end_ms

    def _bar_start(i: int) -> int:
        # align to the first downbeat at or after the section start
        return beats[i].time_ms

    if cadence == "section":
        return [(section.start_ms, sec_end)]

    if cadence == "bar":
        for i, b in enumerate(beats):
            if b.index_in_bar == 1:
                # end = first beat of next bar, else section end
                j = i + meter
                end = beats[j].time_ms if j < len(beats) else sec_end
                out.append((_bar_start(i), min(end, sec_end)))
        return out

    if cadence == "downbeat":
        for i, b in enumerate(beats):
            if b.index_in_bar == 1:
                j = i + meter
                end = beats[j].time_ms if j < len(beats) else sec_end
                start = b.time_ms
                out.append((start, min(end, sec_end)))
        return out

    if cadence == "beat":
        for i, b in enumerate(beats):
            nxt = beats[i + 1].time_ms if i + 1 < len(beats) else sec_end
            out.append((b.time_ms, min(nxt, sec_end)))
        return out

    if cadence == "half_beat":
        for i, b in enumerate(beats):
            nxt = beats[i + 1].time_ms if i + 1 < len(beats) else sec_end
            mid = b.time_ms + (nxt - b.time_ms) // 2
            out.append((b.time_ms, mid))
            out.append((mid, nxt))
        return out

    raise ValueError(f"unknown cadence: {cadence}")


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------

@dataclass
class PlanOptions:
    frame_ms: int = 50
    density_scale: float = 1.0        # multiplies per-model effect budgets
    max_effects_per_model: int = 400  # safety valve for very long songs
    enforce_span: bool = True


def _snap(ms: int, frame_ms: int) -> int:
    return max(0, int(round(ms / frame_ms)) * frame_ms)


def build_plan(analysis: BeatAnalysis, models: Sequence[str], style: Style,
               options: Optional[PlanOptions] = None) -> SequencePlan:
    """Build the effect plan. *models* is the model+group name list from xLights."""
    opts = options or PlanOptions()
    density_mult = {"sparse": 0.7, "medium": 1.0, "dense": 1.3}.get(style.density, 1.0)
    density_mult *= opts.density_scale

    models = list(dict.fromkeys(models))  # de-dupe, keep order
    roles: Dict[str, str] = {m: infer_model_role(m) for m in models}
    by_role: Dict[str, List[str]] = {}
    for m, r in roles.items():
        by_role.setdefault(r, []).append(m)

    # Which models each slot role will target.
    def targets_for(slot_role: str) -> List[str]:
        picked: List[str] = []
        for r in _TARGETS.get(slot_role, ("generic",)):
            for m in by_role.get(r, []):
                if m not in picked:
                    picked.append(m)
        if not picked:  # no model matched any preferred role: spread everything
            picked = list(models)
        return picked

    # Layer occupancy per model so nothing overlaps on the same layer.
    occupancy: Dict[Tuple[str, int], List[Tuple[int, int]]] = {}
    per_model_count: Dict[str, int] = {}

    def _fits(model: str, layer: int, s: int, e: int) -> bool:
        for (s0, e0) in occupancy.get((model, layer), ()):
            if s < e0 and s0 < e:
                return False
        return True

    def _place(model: str, layer: int, s: int, e: int) -> int:
        """Find a free layer >= layer; record and return the used layer."""
        lay = layer
        while not _fits(model, lay, s, e):
            lay += 1
        occupancy.setdefault((model, lay), []).append((s, e))
        occupancy[(model, lay)].sort()
        per_model_count[model] = per_model_count.get(model, 0) + 1
        return lay

    placements: List[Placement] = []

    for slot in style.slots:
        for section in analysis.sections:
            if slot.sections is not None and section.label not in slot.sections:
                continue
            windows = _cadence_windows(analysis, slot.cadence, section)
            if not windows:
                continue

            # Density gating: dense cadences thin out on low-energy sections.
            # (Whether a detail slot plays in a quiet section at all is decided
            # by its own `sections` filter, not here.)
            keep = 1.0
            if slot.cadence in ("beat", "half_beat"):
                keep = {"low": 0.5, "mid": 0.85, "high": 1.0}.get(section.label, 1.0)
            keep *= min(1.0, density_mult)
            if keep < 0.999 and slot.cadence in ("beat", "half_beat"):
                stride = 2 if keep <= 0.55 else 1
                windows = windows[::stride] if stride > 1 else windows

            models_for_slot = targets_for(slot.role)
            # In sparse styles, background-style slots skip some models so the
            # display breathes; accents/outline always play.
            if density_mult < 0.85 and slot.role == ROLE_DETAIL:
                models_for_slot = models_for_slot[: max(1, len(models_for_slot) // 2)]

            for idx, (w_start, w_end) in enumerate(windows):
                if w_end <= w_start:
                    continue
                for m_idx, model in enumerate(models_for_slot):
                    # Round-robin model order per window so long cadences don't
                    # always hammer the same model first.
                    model = models_for_slot[(m_idx + idx) % len(models_for_slot)]
                    if per_model_count.get(model, 0) >= opts.max_effects_per_model:
                        continue
                    # For non-section cadences on many models, stagger: each
                    # model plays every Nth window (N = models count) for beat
                    # cadences, so per-model density stays sane.
                    if slot.cadence in ("beat", "half_beat", "downbeat") \
                            and len(models_for_slot) > 3:
                        share = max(1, len(models_for_slot) // 2)
                        if (idx + m_idx) % share != 0:
                            continue
                    s = _snap(w_start, opts.frame_ms)
                    e = _snap(w_end, opts.frame_ms)
                    if e <= s:
                        e = s + opts.frame_ms
                    layer = _place(model, slot.layer, s, e)
                    placements.append(Placement(
                        model=model,
                        effect=slot.effect,
                        start_ms=s,
                        end_ms=e,
                        layer=layer,
                        settings=dict(slot.settings),
                        palette=_palette_dict(style, slot.palette_slots),
                        reason=f"{style.id}/{slot.role} @ {slot.cadence} [{section.name}]",
                    ))

    # Guarantee every model lights up at least once: give dark models a
    # section-long background effect using the style's first slot.
    if models and style.slots:
        fallback = next((s for s in style.slots if s.role == ROLE_BACKGROUND),
                        style.slots[0])
        for model in models:
            if per_model_count.get(model, 0) > 0:
                continue
            for section in analysis.sections:
                s = _snap(section.start_ms, opts.frame_ms)
                e = _snap(section.end_ms, opts.frame_ms)
                if e <= s:
                    continue
                layer = _place(model, fallback.layer, s, e)
                placements.append(Placement(
                    model=model, effect=fallback.effect, start_ms=s, end_ms=e,
                    layer=layer, settings=dict(fallback.settings),
                    palette=_palette_dict(style, fallback.palette_slots),
                    reason=f"{style.id}/safety-fill [{section.name}]",
                ))

    placements.sort(key=lambda p: (p.start_ms, p.model, p.layer))

    meta = {
        "generator": "xlights-ai-agent",
        "style": style.id,
        "style_name": style.name,
        "density": style.density,
        "bpm": round(analysis.bpm, 2),
        "meter": analysis.meter,
        "duration_ms": analysis.duration_ms,
        "song": analysis.path,
        "models": len(models),
        "frame_ms": opts.frame_ms,
        "sections": [{"name": s.name, "label": s.label,
                      "start_ms": s.start_ms, "end_ms": s.end_ms}
                     for s in analysis.sections],
    }
    return SequencePlan(meta=meta, placements=placements)
