"""Native xLights AI plugin: build the DLL and run the host harness.

Skipped automatically when g++/binutils aren't available (e.g. Windows CI).
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
import urllib.request

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN_DIR = os.path.join(REPO, "ai_plugins", "AIAutoSequencer")
SO_PATH = os.path.join(PLUGIN_DIR, "build", "AIAutoSequencer.so")
HARNESS_SRC = os.path.join(REPO, "tests", "native", "host_harness.cpp")

needs_compiler = pytest.mark.skipif(
    shutil.which("g++") is None or shutil.which("nm") is None,
    reason="g++/binutils not available",
)


def _build():
    subprocess.run(["bash", os.path.join(PLUGIN_DIR, "build.sh")],
                   check=True, capture_output=True, timeout=180)
    assert os.path.isfile(SO_PATH)


def _build_harness(dest: str) -> str:
    subprocess.run(
        ["g++", "-std=c++17", "-O2",
         "-I" + PLUGIN_DIR,
         "-I" + os.path.join(PLUGIN_DIR, "third_party", "xlights"),
         HARNESS_SRC, "-ldl", "-o", dest],
        check=True, capture_output=True, timeout=180)
    return dest


@needs_compiler
def test_plugin_builds_and_exports():
    _build()
    out = subprocess.run(["nm", "-D", SO_PATH], check=True,
                         capture_output=True, text=True).stdout
    assert "xlCreateAIService" in out
    assert "xlDestroyAIService" in out


@needs_compiler
def test_host_harness_offline_fallback(tmp_path):
    _build()
    harness = _build_harness(str(tmp_path / "host_harness"))
    proc = subprocess.run([harness, SO_PATH], capture_output=True,
                          text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert "harness OK" in proc.stdout
    # SaveSettings must have written the conf file (agent is down here, so the
    # built-in fallback answered -- visible because it has no "Why:" line).
    cfg = os.path.join(os.path.expanduser("~"), ".config", "xlights-ai-agent",
                       "AIAutoSequencer.conf")
    assert os.path.isfile(cfg), cfg
    conf = open(cfg).read()
    assert "enablePrompt=" in conf and "agentUrl=" in conf


@needs_compiler
def test_agent_forwarding(tmp_path):
    """With the agent running, the DLL must use it instead of the fallback."""
    _build()
    harness = _build_harness(str(tmp_path / "host_harness"))

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()

    env = dict(os.environ)
    env["PYTHONPATH"] = os.path.join(REPO, "src")
    python = shutil.which("python3") or "python3"
    # prefer the project venv if present
    venv_py = os.path.join(REPO, ".venv", "bin", "python")
    if os.path.isfile(venv_py):
        python = venv_py
    server = subprocess.Popen(
        [python, "-m", "xlights_ai_agent.cli", "serve", "--port", str(port)],
        env=env, cwd=REPO, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        base = f"http://127.0.0.1:{port}"
        for _ in range(60):
            try:
                urllib.request.urlopen(base + "/agent/ping", timeout=0.3)
                break
            except Exception:
                time.sleep(0.1)
        else:
            pytest.fail("agent did not start")

        proc = subprocess.run(
            [harness, SO_PATH, base], capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, proc.stderr
        assert "harness OK" in proc.stdout
        assert "answer-source: agent" in proc.stdout, \
            "harness should have got the agent's answer"
    finally:
        server.terminate()
        server.wait(timeout=10)


@needs_compiler
def test_palette_forwarding_via_agent(tmp_path):
    """GenerateColorPalette must round-trip through the agent /v1/palette."""
    _build()
    harness = _build_harness(str(tmp_path / "host_harness"))

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()

    env = dict(os.environ)
    env["PYTHONPATH"] = os.path.join(REPO, "src")
    python = shutil.which("python3") or "python3"
    venv_py = os.path.join(REPO, ".venv", "bin", "python")
    if os.path.isfile(venv_py):
        python = venv_py
    server = subprocess.Popen(
        [python, "-m", "xlights_ai_agent.cli", "serve", "--port", str(port)],
        env=env, cwd=REPO, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        base = f"http://127.0.0.1:{port}"
        for _ in range(60):
            try:
                urllib.request.urlopen(base + "/agent/ping", timeout=0.3)
                break
            except Exception:
                time.sleep(0.1)
        else:
            pytest.fail("agent did not start")
        # Ask the agent palette for the same prompt the harness uses
        # (GenerateColorPalette("winter wonderland ice")).
        req = urllib.request.Request(
            base + "/v1/palette", data=json.dumps({"prompt": "winter wonderland ice"}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as r:
            agent_palette = json.loads(r.read())
        proc = subprocess.run(
            [harness, SO_PATH, base], capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, proc.stderr
        # The harness prints the first colour; it must match the agent's first.
        assert agent_palette["colors"][0]["hexValue"] in proc.stdout
    finally:
        server.terminate()
        server.wait(timeout=10)
