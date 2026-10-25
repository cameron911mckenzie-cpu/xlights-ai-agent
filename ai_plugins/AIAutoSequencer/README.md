# AI Auto Sequencer — xLights AI plugin DLL

A third-party **xLights AI service** built on the `aiPlugin.h` C ABI. Drop the
built library into the `ai_plugins/` folder next to the xLights executable and
it appears in **Preferences → Services** as *AI Auto Sequencer* with two
capabilities:

| Capability | What it answers |
|---|---|
| **Prompt** | Style suggestions & sequencing advice for free-form prompts |
| **Color Palette** | 8-colour palettes for the AI colour-palette dialog |

Requests are forwarded over HTTP to the local agent:

```
POST {AgentUrl}/v1/prompt   {"prompt": "..."}     -> {"text": "..."}
POST {AgentUrl}/v1/palette  {"prompt": "..."}     -> {"description", "colors": [{hexValue,name,description}]}
GET  {AgentUrl}/agent/ping                        -> liveness
```

Start the agent with:

```bash
xlights-ai serve            # default http://127.0.0.1:8756
```

When the agent isn't running, the plugin falls back to a small **built-in
keyword style engine** (toggle *Built-in offline fallback* in the service
settings), so xLights always gets a useful answer.

## Build

```bash
./build.sh                  # plain g++/clang++  -> build/AIAutoSequencer.so
# or
cmake -B build && cmake --build build          # -> build/AIAutoSequencer.so/.dll/.dylib
# or, straight into an xLights install:
cmake -B build -DXLIGHTS_DIR=/path/to/xLights && cmake --build build --target install
```

No dependencies beyond a C++17 compiler and OS sockets — the plugin never links
back into xLights (that's the point of the C ABI).

## Install

Copy the library into **`<xLights folder>/ai_plugins/`** (same folder as the
`xLights` / `xLights.exe` binary):

```
Linux:   AIAutoSequencer.so
Windows: AIAutoSequencer.dll
macOS:   AIAutoSequencer.dylib
```

Restart xLights → *Preferences → Services* → **AI Auto Sequencer**.

## Settings

Editable from the Services page (persisted to
`~/.config/xlights-ai-agent/AIAutoSequencer.conf`, `%APPDATA%` on Windows):

* **Agent URL** — base URL of `xlights-ai serve` (default `http://127.0.0.1:8756`)
* **Request timeout (ms)** — give-up time before the fallback answers
* **Enable Prompt / Enable Color Palette** — capability toggles
* **Built-in offline fallback** — answer without the agent

## Test

```bash
# from the repo root
.venv/bin/pytest tests/test_native_plugin.py -v
# or manually:
ai_plugins/AIAutoSequencer/build.sh
g++ -std=c++17 -Iai_plugins/AIAutoSequencer \
    -Iai_plugins/AIAutoSequencer/third_party/xlights \
    tests/native/host_harness.cpp -ldl -o /tmp/harness
/tmp/harness ai_plugins/AIAutoSequencer/build/AIAutoSequencer.so
/tmp/harness ai_plugins/AIAutoSequencer/build/AIAutoSequencer.so http://127.0.0.1:8756
```

The harness mimics `ServiceManager::loadPlugins`: `dlopen`, resolve
`xlCreateAIService`/`xlDestroyAIService`, create with a null `ServiceManager`,
and exercise prompts, palettes, settings and property setters.

## Files

```
AIAutoSequencerService.h/.cpp   the service (HTTP client + built-in fallback)
CMakeLists.txt / build.sh       builds
third_party/xlights/            ABI headers vendored from xLights (GPLv3, unmodified)
../../tests/native/host_harness.cpp   host-side ABI test
```
