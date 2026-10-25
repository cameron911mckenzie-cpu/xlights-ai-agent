# xLights AI Auto Sequencer

An **addon for [xLights](https://www.xlights.org)** that automatically sequences songs
**from the music beats** — and lets you drive the look with **prompts and style
suggestions**.

Give it a song and a prompt like *"spooky halloween with aggressive strobes on the
chorus"*, and it will:

1. **Analyse the music** — tempo, beat grid, downbeats, and sections
   (intro / verse / chorus / bridge) straight from the audio file.
2. **Suggest a style** — a complete show look: 8-colour palette, effect recipes per
   model class, cadence (beat / downbeat / bar / section) and density — from your
   prompt (keyword engine built in, optional LLM-backed refinement).
3. **Plan the sequence** — beat-accurate effect placements across *your* layout's
   models and groups, layered, frame-aligned, section-aware.
4. **Push it into xLights** — over xLights' own automation API (or its built-in MCP
   server), then render and save. No hand-dragging effects onto the grid.

```
$ xlights-ai sequence "Carol of the Bells.mp3" --prompt "candy cane rush, red and white" --render --save
[analyzed] {"bpm": 138.4, "beats": 371, "sections": ["intro", "verse", "chorus", ...], "confidence": 0.81}
[style]    {"style": "candy-cane-rush", "density": "dense", "reason": "keywords 'candy', 'rush' ..."}
[planned]  {"placements": 1842, "models": 14, "effects": 6, "max_layer": 2}
[pushed]   {"added": 1842, "failed": 0}
```

---

## What's in the box

| Piece | What it does |
|---|---|
| **`xlights-ai` CLI** | `analyze` · `suggest` · `styles` · `sequence` · `status` · `serve` |
| **Beat engine** | STFT spectral-flux onsets → tempo (with octave-safe prior) → dynamic-programming beat tracking → low-band downbeat detection → energy sections. WAV natively, anything else via `ffmpeg`. Import/export xLights `*_beats.txt`. |
| **Style engine** | 10 bundled styles (Christmas Classic, Winter Wonderland, Electro Drop, Spooky Halloween, Synthwave Retro, …) with real xLights effect names and settings; prompt colours and mood words rewrite palette and density. Optional LLM refinement (OpenAI-compatible / Ollama / Anthropic). |
| **Planner** | Maps style slots onto your models by inferred role (tree/matrix → background, outline/arch → chase, star/sign → accent…), stamps them across beat cadences, stacks layers instead of overlapping, snaps to the frame grid, guarantees no model is left dark. |
| **xLights client** | REST `POST /xlDoAutomation` (default) or `POST /mcp` MCP transport; port discovery (49913/49914), `getModels`, `addEffect`, `clearModelEffects`, `renderAll`, `saveSequence`, plan pushing with progress. |
| **Local HTTP agent** | `xlights-ai serve` — JSON endpoints for prompt/palette/analyze/plan/sequence, used by the plugin DLL and any other tool. |
| **xLights AI plugin DLL** | `ai_plugins/AIAutoSequencer/` — a real xLights AI service (`xlCreateAIService` C ABI) that shows up in **Preferences → Services**, answers *Prompt* and *Color Palette* requests via the local agent (with an offline built-in fallback). |

---

## Install

```bash
git clone <this repo> && cd xlights-ai-agent
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
# optional: ffmpeg for MP3/OGG/FLAC/M4A input (WAV works without it)
```

## Quick start

```bash
# 1. What does the song look like? (BPM, sections, beats)
xlights-ai analyze "song.mp3"
xlights-ai analyze "song.mp3" --beats-out "song_beats.txt"   # xLights beats file

# 2. What style should it be?
xlights-ai styles
xlights-ai suggest "winter wonderland, icy and calm"
xlights-ai suggest "EDM drop, aggressive, blue and white strobes" --json

# 3. Auto-sequence it (xLights running with automation enabled):
xlights-ai status                                   # find the running instance
xlights-ai sequence "song.mp3" --prompt "spooky halloween" --replace --render --save

# ...or without xLights: just produce the plan file
xlights-ai sequence "song.mp3" --style electro-drop \
    --model MegaTree --model RoofOutline --model "All House" \
    --dry-run --plan-out show-plan.json
```

### Using xLights' beat files instead of re-analysing

xLights already ships `*_beats.txt` files (and can generate its own via VAMP
plugins: *Tools → Beat Timing*). The agent reads them directly:

```bash
xlights-ai sequence "song.mp3" --beats "song_beats.txt" --prompt "christmas classic"
```

---

## xLights setup

1. **Enable the automation service** in xLights: *Preferences → xFade / xLights
   Automation* (port **49913** for instance A, **49914** for instance B).
2. Open your sequence (or let the agent open/create it).
3. Run `xlights-ai status` to confirm connectivity, then `xlights-ai sequence …`.

Two transports are supported — `--transport rest` (default, the
`POST /xlDoAutomation` command dispatcher) and `--transport mcp` (xLights' built-in
Model Context Protocol endpoint at `POST /mcp`, same port). Both are documented in
xLights' `documentation/xlDo Commands.txt`.

### The plugin DLL (in-app AI service)

The plugin makes the addon a first-class xLights citizen: it appears in
**Preferences → Services** as *AI Auto Sequencer* with **Prompt** and **Color
Palette** capabilities, so xLights' own AI prompts and the AI colour-palette dialog
can route through it.

```bash
cd ai_plugins/AIAutoSequencer
./build.sh                       # or: cmake -B build && cmake --build build
# Linux:   build/AIAutoSequencer.so       -> <xLights dir>/ai_plugins/
# Windows: build/AIAutoSequencer.dll      -> <xLights dir>/ai_plugins/
# macOS:   build/AIAutoSequencer.dylib (cmake) or libAIAutoSequencer.dylib (build.sh)
```

The `ai_plugins/` folder sits **next to the xLights executable**. With the plugin
installed, start the agent (`xlights-ai serve`) for full behaviour; without the
agent running it degrades gracefully to a built-in keyword style engine.

Settings (agent URL, timeouts, capability toggles) persist in
`~/.config/xlights-ai-agent/AIAutoSequencer.conf` (or `%APPDATA%` on Windows) and
are editable from the xLights Services page.

---

## Prompts & style suggestions

`xlights-ai suggest "…"` (and the agent's `/v1/prompt`) accept free-form text:

* **Genre / theme keywords** pick the base style — `halloween`, `winter`, `80s`,
  `edm`, `christmas`, `gold`, `rainbow`, `space`, `candy`, …
* **Colour words** (`red`, `green`, `gold`, `purple`, `cyan`, `magenta`, …) rewrite
  the 8-colour palette.
* **Mood words** set density: `aggressive / fast / strobe` → *dense*,
  `calm / gentle / slow` → *sparse*.
* Explicit control: `--style electro-drop` pins a style; `--density 0.5` scales
  effect counts; `--bpm 128` locks the tempo.

### Optional LLM brain

Point the agent at any chat model and prompts get a whole extra level of nuance
(the rule engine remains the validated fallback):

```bash
# OpenAI-compatible (OpenAI, LM Studio, vLLM, OpenRouter, ...)
export XLAI_LLM_PROVIDER=openai
export XLAI_LLM_BASE_URL=https://api.openai.com/v1
export XLAI_LLM_API_KEY=sk-...
export XLAI_LLM_MODEL=gpt-4o-mini

# or local Ollama
export XLAI_LLM_PROVIDER=ollama XLAI_LLM_MODEL=llama3.1

# or Anthropic
export XLAI_LLM_PROVIDER=anthropic XLAI_LLM_API_KEY=sk-ant-...

xlights-ai suggest "a nursery-rhyme light show for toddlers" --llm
xlights-ai sequence song.mp3 --prompt "gothic cathedral, cold and grand" --llm
```

The LLM is asked for structured JSON (palette + per-role effects using real
xLights effect names), which is validated against the known effect list before
anything reaches your sequence.

---

## Local HTTP agent

```bash
xlights-ai serve           # 127.0.0.1:8756  (use --host 0.0.0.0 for the LAN)
```

| Endpoint | Body | Returns |
|---|---|---|
| `GET /agent/ping` | – | `{"status":"ok",...}` (used by the plugin) |
| `POST /v1/prompt` | `{prompt, style?, llm?}` | style-suggestion text + JSON |
| `POST /v1/palette` | `{prompt}` | `{description, colors:[{hexValue,name,...}]}` |
| `POST /v1/analyze` | `{audio_path \| beats_path, meter?}` | beats, BPM, sections |
| `POST /v1/plan` | `{audio_path, prompt?, style?, models}` | full placement plan |
| `POST /v1/sequence` | `{audio_path, prompt?, host?, replace?, render?, ...}` | plan + push result |

---

## How it hooks into xLights (summary)

* **AI plugin ABI** — xLights loads `*.dll|so|dylib` from `ai_plugins/` next to the
  executable, each exporting `xlCreateAIService` / `xlDestroyAIService`
  (`src-core/ai/aiPlugin.h`). The addon's service subclasses `aiBase` (PROMPT +
  COLORPALETTES capabilities) with zero linkage back into xLights.
* **Automation API** — `POST /xlDoAutomation` on the xFade port executes commands
  like `getModels`, `addEffect`, `clearModelEffects`, `renderAll`, `saveSequence`
  (`src-ui-wx/automation/`, documented in `documentation/xlDo Commands.txt`).
* **MCP server** — the same port serves `POST /mcp` with tools such as
  `addEffect`, `getModels`, `renderAll` (`src-ui-wx/automation/xLightsMCPServer.cpp`).
* **Beat files** — `*_beats.txt` rows are `start<TAB>end<TAB>beat_in_bar`; the
  agent reads and writes exactly that format.

See [docs/INTEGRATION.md](docs/INTEGRATION.md) for the full reference.

## Development

```bash
.venv/bin/pytest                 # 70+ tests incl. native plugin ABI harness
ai_plugins/AIAutoSequencer/build.sh
```

Layout: `src/xlights_ai_agent/` (audio, styles, llm, planner, xlights_client,
sequencer, serve, cli) · `ai_plugins/AIAutoSequencer/` (C++ plugin) · `tests/`.

## License

GPL-3.0-or-later (see [LICENSE](LICENSE)) — the same license as xLights, whose
AI-plugin ABI headers are vendored under `ai_plugins/AIAutoSequencer/third_party/xlights/`
with their original copyright notices.
