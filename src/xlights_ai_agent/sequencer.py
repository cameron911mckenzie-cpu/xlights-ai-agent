"""The end-to-end auto-sequencing pipeline.

    audio -> beats -> style suggestion -> plan -> (push to xLights) -> render

``run_pipeline`` ties every module together and is what the CLI and the local
HTTP agent (``xlights-ai serve``) both call. Each stage is skippable/overridable
so the same pipeline powers dry runs, plan files and live sequencing.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence

from .audio import BeatAnalysis, analyze_beats, decode_audio, load_beats_txt
from .planner import PlanOptions, SequencePlan, build_plan
from .styles import StyleSuggestion, suggest_style
from .xlights_client import XLightsClient, XLightsError

__all__ = ["SequenceRequest", "SequenceResult", "run_pipeline"]


@dataclass
class SequenceRequest:
    """Everything the pipeline needs to do its job."""

    audio_path: Optional[str] = None       # song to analyse
    beats_path: Optional[str] = None       # or an existing xLights *_beats.txt
    prompt: str = ""                       # free-form style prompt
    style_id: Optional[str] = None         # explicit style overrides the prompt
    meter: int = 4                         # beats per bar
    bpm_hint: Optional[float] = None       # lock the tempo if known
    models: Optional[List[str]] = None     # override the layout model list
    include_groups: bool = True
    replace: bool = False                  # clear target models first
    render: bool = False                   # render after pushing
    save: bool = False                      # save the sequence after pushing
    save_as: Optional[str] = None
    new_sequence: Optional[str] = None     # create a sequence for this media first
    frame_ms: int = 50
    use_llm: bool = False
    plan_out: Optional[str] = None         # write the plan JSON here
    dry_run: bool = False                  # never touch xLights
    density_scale: float = 1.0


@dataclass
class SequenceResult:
    analysis: BeatAnalysis
    suggestion: StyleSuggestion
    plan: SequencePlan
    push: Optional[Dict[str, object]] = None
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "analysis": self.analysis.summary(),
            "suggestion": self.suggestion.to_dict(),
            "stats": self.plan.stats(),
            "push": self.push,
            "notes": self.notes,
        }


def _load_analysis(req: SequenceRequest) -> BeatAnalysis:
    if req.beats_path:
        duration = None
        if req.audio_path and os.path.isfile(req.audio_path):
            try:
                duration = decode_audio(req.audio_path).duration_s
            except Exception:  # noqa: BLE001 - beats alone are fine
                duration = None
        analysis = load_beats_txt(req.beats_path, meter=req.meter,
                                  duration_s=duration)
        return analysis
    if not req.audio_path:
        raise ValueError("either audio_path or beats_path is required")
    audio = decode_audio(req.audio_path)
    return analyze_beats(audio, meter=req.meter, bpm_hint=req.bpm_hint)


def run_pipeline(req: SequenceRequest,
                 client: Optional[XLightsClient] = None,
                 progress: Optional[Callable[[str, Dict], None]] = None,
                 model_source: Optional[Sequence[str]] = None) -> SequenceResult:
    """Run analyse -> suggest -> plan -> (push).

    *model_source* lets tests inject a model list without a live xLights.
    With *client* set and *req.dry_run* false, the plan is pushed (and
    optionally rendered/saved) on the connected instance.
    """
    notes: List[str] = []

    def report(stage: str, info: Dict) -> None:
        if progress is not None:
            progress(stage, info)

    # 1. beats ---------------------------------------------------------------
    analysis = _load_analysis(req)
    report("analyzed", {"bpm": round(analysis.bpm, 2),
                        "beats": len(analysis.beats),
                        "sections": [s.name for s in analysis.sections],
                        "confidence": round(analysis.confidence, 3)})
    if analysis.confidence < 0.25:
        notes.append("beat confidence is low -- consider --bpm to lock the tempo")

    # 2. style ---------------------------------------------------------------
    suggestion = suggest_style(req.prompt, explicit_style=req.style_id,
                               use_llm=req.use_llm)
    report("style", {"style": suggestion.style.id, "source": suggestion.source,
                     "density": suggestion.style.density,
                     "reason": suggestion.reason})
    if suggestion.tweaks:
        notes.extend(suggestion.tweaks)

    # 3. models --------------------------------------------------------------
    if req.models is not None:
        models = list(req.models)
        notes.append("using explicit model list from the request")
    elif model_source is not None:
        models = list(model_source)
    elif client is not None and not req.dry_run:
        models = client.get_models(include_groups=req.include_groups)
        if not models:
            raise XLightsError("xLights returned no models -- check the Layout tab")
    else:
        raise XLightsError(
            "no models available: connect to xLights or pass --model (repeatable)"
        )

    # 4. plan ----------------------------------------------------------------
    plan = build_plan(
        analysis, models, suggestion.style,
        options=PlanOptions(frame_ms=req.frame_ms,
                            density_scale=req.density_scale),
    )
    report("planned", plan.stats())

    if req.plan_out:
        with open(req.plan_out, "w", encoding="utf-8") as fh:
            json.dump(plan.to_dict(), fh, indent=2)
        notes.append(f"plan written to {req.plan_out}")

    result = SequenceResult(analysis=analysis, suggestion=suggestion,
                            plan=plan, notes=notes)

    # 5. push ----------------------------------------------------------------
    if req.dry_run or client is None:
        if req.dry_run:
            notes.append("dry run: nothing was sent to xLights")
        return result

    if req.new_sequence:
        client.new_sequence(media=req.new_sequence, frame_ms=req.frame_ms)
        notes.append(f"created sequence for {req.new_sequence}")

    def push_progress(i: int, total: int, p) -> None:
        if i % 25 == 0 or i == total:
            report("pushing", {"done": i, "total": total})

    push = client.push_plan(plan, replace=req.replace, progress=push_progress)
    result.push = push
    report("pushed", {k: v for k, v in push.items() if k != "cleared"})
    if push.get("failed"):
        notes.append(f"{push['failed']} placements failed"
                     + (f": {push.get('last_error')}" if push.get("last_error") else ""))

    if req.render:
        client.render()
        notes.append("sequence rendered")
    if req.save:
        client.save_sequence(req.save_as)
        notes.append(f"sequence saved as {req.save_as}" if req.save_as
                     else "sequence saved")
    return result
