"""``xlights-ai`` command line interface.

Subcommands:

    status     discover the running xLights automation service and show info
    analyze    detect beats/tempo/sections of a song (optionally write *_beats.txt)
    styles     list the bundled show styles
    suggest    suggest a style (and palette) for a free-form prompt
    sequence   full auto-sequencer: analyse -> suggest -> plan -> push -> render
    serve      run the local HTTP agent used by the xLights AI plugin DLL
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import List, Optional

from . import __version__
from .audio import analyze_beats, decode_audio, load_beats_txt, save_beats_txt
from .sequencer import SequenceRequest, run_pipeline
from .styles import list_styles, suggest_style
from .xlights_client import XLightsError, discover


def _print_json(obj) -> None:
    print(json.dumps(obj, indent=2))


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def _cmd_status(args) -> int:
    conn = discover(host=args.host,
                    ports=(args.port,) if args.port else (49913, 49914),
                    transport=args.transport)
    from .xlights_client import XLightsClient  # local to keep import graph tidy
    client = XLightsClient(conn)
    _print_json(client.status())
    return 0


def _cmd_analyze(args) -> int:
    if args.beats:
        analysis = load_beats_txt(args.beats, meter=args.meter)
    else:
        audio = decode_audio(args.audio)
        analysis = analyze_beats(audio, meter=args.meter,
                                 bpm_hint=args.bpm)
    if args.beats_out:
        save_beats_txt(analysis, args.beats_out)
        print(f"wrote xLights beats file: {args.beats_out}", file=sys.stderr)
    if args.json:
        _print_json(analysis.summary())
        return 0
    print(f"song      : {analysis.path}")
    print(f"tempo     : {analysis.bpm:.1f} BPM  (meter {analysis.meter}/4, "
          f"confidence {analysis.confidence:.2f})")
    print(f"duration  : {analysis.duration_s:.1f}s   beats: {len(analysis.beats)} "
          f"({len(analysis.downbeats)} downbeats)")
    for s in analysis.sections:
        print(f"  section {s.start_ms:7d}-{s.end_ms:7d} ms  {s.name:<6} "
              f"({s.label}, energy {s.energy:.2f})")
    return 0


def _cmd_styles(args) -> int:
    styles = list_styles()
    if args.json:
        _print_json([
            {"id": s.id, "name": s.name, "description": s.description,
             "density": s.density, "keywords": list(s.keywords),
             "palette": list(s.palette),
             "effects": [{"role": sl.role, "effect": sl.effect,
                          "cadence": sl.cadence} for sl in s.slots]}
            for s in styles])
        return 0
    for s in styles:
        print(f"{s.id:<18} {s.name:<20} [{s.density:<6}] {s.description}")
    return 0


def _cmd_suggest(args) -> int:
    suggestion = suggest_style(args.prompt, explicit_style=args.style,
                               use_llm=args.llm)
    if args.json:
        _print_json(suggestion.to_dict())
        return 0
    s = suggestion.style
    print(f"style     : {s.name} ({s.id})"
          + (f"  [from {suggestion.base_id}]" if s.id != suggestion.base_id else ""))
    print(f"source    : {suggestion.source}")
    print(f"why       : {suggestion.reason}")
    print(f"density   : {s.density}")
    print(f"palette   : {'  '.join(s.palette)}")
    for slot in s.slots:
        secs = ",".join(slot.sections) if slot.sections else "all"
        print(f"  {slot.role:<11} {slot.effect:<12} every {slot.cadence:<9} "
              f"(sections {secs}, layer {slot.layer})  {slot.why}")
    if s.notes:
        print(f"notes     : {s.notes}")
    for t in suggestion.tweaks:
        print(f"tweak     : {t}")
    return 0


def _cmd_sequence(args) -> int:
    req = SequenceRequest(
        audio_path=None if args.beats else args.audio,
        beats_path=args.beats,
        prompt=args.prompt or "",
        style_id=args.style,
        meter=args.meter,
        bpm_hint=args.bpm,
        models=args.model or None,
        replace=args.replace,
        render=args.render,
        save=args.save,
        save_as=args.save_as,
        new_sequence=args.new_sequence,
        frame_ms=args.frame_ms,
        use_llm=args.llm,
        plan_out=args.plan_out,
        dry_run=args.dry_run,
        density_scale=args.density,
    )

    client = None
    conn = None
    if not args.dry_run:
        try:
            conn = discover(host=args.host,
                            ports=(args.port,) if args.port else (49913, 49914),
                            transport=args.transport)
        except XLightsError as exc:
            print(f"error: {exc}", file=sys.stderr)
            print("hint : run with --dry-run --plan-out plan.json to generate "
                  "the sequence without xLights, or --model NAME to plan for "
                  "known models.", file=sys.stderr)
            return 1
        from .xlights_client import XLightsClient
        client = XLightsClient(conn)

    def progress(stage: str, info: dict) -> None:
        if args.quiet:
            return
        if stage == "pushing":
            print(f"  push {info['done']}/{info['total']}", file=sys.stderr)
        else:
            print(f"[{stage}] {json.dumps(info, default=str)}", file=sys.stderr)

    try:
        result = run_pipeline(req, client=client, progress=progress)
    except (XLightsError, ValueError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        _print_json(result.to_dict())
        return 0
    a, s, p = result.analysis, result.suggestion, result.plan
    print(f"beats     : {len(a.beats)} @ {a.bpm:.1f} BPM "
          f"(confidence {a.confidence:.2f})")
    print(f"style     : {s.style.name} ({s.style.id}, {s.source})")
    st = p.stats()
    print(f"plan      : {st['placements']} effects on {st['models']} models/"
          f"groups, {st['effects']} effect types, max layer {st['max_layer']}")
    if result.push:
        print(f"pushed    : {result.push['added']} added"
              + (f", {result.push['failed']} failed" if result.push.get("failed")
                 else "")
              + (f", cleared {len(result.push['cleared'])} models"
                 if result.push.get("cleared") else ""))
    for note in result.notes:
        print(f"note      : {note}")
    if args.plan_out:
        print(f"plan file : {args.plan_out}")
    return 0 if not (result.push and result.push.get("failed")) else 1


def _cmd_serve(args) -> int:
    from .serve import serve
    serve(host=args.host, port=args.port, verbose=args.verbose)
    return 0


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def _add_connection_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--host", default="127.0.0.1",
                   help="xLights automation host (default: 127.0.0.1)")
    p.add_argument("--port", type=int, default=0,
                   help="xLights automation port (default: probe 49913/49914)")
    p.add_argument("--transport", choices=("rest", "mcp"), default="rest",
                   help="command transport: REST xlDoAutomation (default) or MCP")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="xlights-ai",
        description="AI auto-sequencer addon for xLights: sequence songs from "
                    "their music beats, with prompt-driven style suggestions.")
    ap.add_argument("--version", action="version", version=f"xlights-ai {__version__}")
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("status", help="show the connected xLights instance")
    _add_connection_args(p)
    p.set_defaults(func=_cmd_status)

    p = sub.add_parser("analyze", help="detect beats/tempo/sections of a song")
    p.add_argument("audio", nargs="?", help="audio file (WAV, or anything with ffmpeg)")
    p.add_argument("--beats", help="use an existing xLights *_beats.txt instead")
    p.add_argument("--beats-out", help="also write the beats as xLights *_beats.txt")
    p.add_argument("--meter", type=int, default=4, help="beats per bar (default 4)")
    p.add_argument("--bpm", type=float, help="lock the tempo to this BPM")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_cmd_analyze)

    p = sub.add_parser("styles", help="list bundled show styles")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_cmd_styles)

    p = sub.add_parser("suggest", help="suggest a style for a prompt")
    p.add_argument("prompt", help='e.g. "spooky halloween with lots of strobes"')
    p.add_argument("--style", help="start from an explicit style id")
    p.add_argument("--llm", action="store_true",
                   help="use a configured LLM (XLAI_LLM_* env vars)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_cmd_suggest)

    p = sub.add_parser("sequence", help="auto-sequence a song from its beats")
    p.add_argument("audio", nargs="?", help="audio file to analyse")
    p.add_argument("--beats", help="use an existing xLights *_beats.txt file")
    p.add_argument("--prompt", help="free-form style prompt")
    p.add_argument("--style", help="explicit style id (see `xlights-ai styles`)")
    p.add_argument("--meter", type=int, default=4, help="beats per bar (default 4)")
    p.add_argument("--bpm", type=float, help="lock the tempo to this BPM")
    p.add_argument("--model", action="append",
                   help="target model/group (repeatable; skips getModels)")
    p.add_argument("--replace", action="store_true",
                   help="clear existing effects on target models first")
    p.add_argument("--render", action="store_true", help="render after pushing")
    p.add_argument("--save", action="store_true", help="save after pushing")
    p.add_argument("--save-as", help="save under this sequence name")
    p.add_argument("--new-sequence",
                   help="create a new sequence for this media file first")
    p.add_argument("--frame-ms", type=int, default=50, help="frame grid (default 50)")
    p.add_argument("--density", type=float, default=1.0,
                   help="scale effect density (0.5 = half, 2 = double)")
    p.add_argument("--plan-out", help="write the plan JSON to this path")
    p.add_argument("--dry-run", action="store_true",
                   help="never contact xLights (needs --model or --plan-out)")
    p.add_argument("--llm", action="store_true", help="let an LLM shape the style")
    p.add_argument("--json", action="store_true")
    p.add_argument("--quiet", action="store_true")
    _add_connection_args(p)
    p.set_defaults(func=_cmd_sequence)

    p = sub.add_parser("serve", help="run the local HTTP agent (for the plugin DLL)")
    p.add_argument("--host", default="127.0.0.1",
                   help="bind address (default 127.0.0.1; use 0.0.0.0 for LAN)")
    p.add_argument("--port", type=int, default=8756)
    p.add_argument("--verbose", action="store_true", help="log each request")
    p.set_defaults(func=_cmd_serve)

    return ap


def main(argv: Optional[List[str]] = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    if args.command == "analyze" and not args.audio and not args.beats:
        ap.error("analyze needs an audio file or --beats FILE")
    if args.command == "sequence" and not args.audio and not args.beats:
        ap.error("sequence needs an audio file or --beats FILE")
    try:
        return args.func(args)
    except XLightsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
