"""Optional LLM backends for prompt-driven style suggestions.

Everything here is standard-library only (``urllib``). Configuration comes
from environment variables so the plugin/CLI can share it:

    XLAI_LLM_PROVIDER = openai | ollama | anthropic      (or XLAI_PROVIDER)
    XLAI_LLM_BASE_URL = https://api.openai.com/v1 | http://localhost:11434 | ...
    XLAI_LLM_API_KEY  = ...                              (not needed for ollama)
    XLAI_LLM_MODEL    = gpt-4o-mini | llama3.1 | claude-...

Legacy OPENAI_* / OLLAMA_* / ANTHROPIC_* variables are honoured as fallbacks.
Any LLM failure raises :class:`LLMError`; callers fall back to rule-based
suggestions, which keeps the sequencer fully usable offline.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Dict, Optional

from .styles import KNOWN_EFFECTS, Style

__all__ = ["LLMConfig", "LLMError", "chat", "suggest_with_llm"]


class LLMError(RuntimeError):
    """Raised when an LLM backend cannot be reached or returns junk."""


@dataclass
class LLMConfig:
    provider: str = "openai"          # openai | ollama | anthropic
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    timeout: float = 60.0

    def is_configured(self) -> bool:
        if self.provider == "ollama":
            return bool(self.base_url or self.model)
        if self.provider == "anthropic":
            return bool(self.api_key)
        return bool(self.base_url or self.api_key)

    @classmethod
    def from_env(cls, env: Optional[Dict[str, str]] = None) -> Optional["LLMConfig"]:
        env = dict(os.environ if env is None else env)

        def first(*keys: str) -> str:
            for k in keys:
                v = env.get(k, "").strip()
                if v:
                    return v
            return ""

        provider = first("XLAI_LLM_PROVIDER", "XLAI_PROVIDER").lower()
        base = first("XLAI_LLM_BASE_URL", "XLAI_BASE_URL")
        key = first("XLAI_LLM_API_KEY", "XLAI_API_KEY")
        model = first("XLAI_LLM_MODEL", "XLAI_MODEL")

        if not provider:
            if first("ANTHROPIC_API_KEY"):
                provider = "anthropic"
            elif first("OLLAMA_HOST", "OLLAMA_BASE_URL"):
                provider = "ollama"
            elif first("OPENAI_API_KEY", "OPENAI_BASE_URL"):
                provider = "openai"
            else:
                return None

        if not base:
            if provider == "anthropic":
                base = "https://api.anthropic.com/v1"
            elif provider == "ollama":
                base = (first("OLLAMA_HOST", "OLLAMA_BASE_URL") or "http://127.0.0.1:11434")
            else:
                base = first("OPENAI_BASE_URL") or "https://api.openai.com/v1"
        if not key:
            if provider == "anthropic":
                key = first("ANTHROPIC_API_KEY")
            else:
                key = first("OPENAI_API_KEY")
        if not model:
            defaults = {"openai": "gpt-4o-mini", "anthropic": "claude-sonnet-4-5",
                        "ollama": "llama3.1"}
            model = defaults.get(provider, "")
        base = base.rstrip("/")
        # Normalise to what chat() appends: openai -> BASE/chat/completions,
        # anthropic -> BASE/messages, ollama -> BASE/api/chat.
        if provider == "ollama":
            if base.endswith("/v1"):
                base = base[: -len("/v1")].rstrip("/")
        elif provider == "anthropic":
            if not base.endswith("/v1"):
                base = base + "/v1"
        else:  # openai / OpenAI-compatible
            if not base.endswith("/v1"):
                base = base + "/v1"
        return cls(provider=provider, base_url=base, api_key=key, model=model)


def _post_json(url: str, payload: dict, headers: Dict[str, str],
               timeout: float) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:400]
        raise LLMError(f"HTTP {exc.code} from {url}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise LLMError(f"cannot reach {url}: {exc}") from exc
    try:
        return json.loads(body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise LLMError(f"non-JSON response from {url}") from exc


def chat(cfg: LLMConfig, system: str, user: str, temperature: float = 0.4) -> str:
    """Single-turn chat completion; returns the assistant text."""
    if cfg.provider == "anthropic":
        url = f"{cfg.base_url}/messages"
        headers = {"x-api-key": cfg.api_key, "anthropic-version": "2023-06-01"}
        payload = {
            "model": cfg.model,
            "max_tokens": 1200,
            "temperature": temperature,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        out = _post_json(url, payload, headers, cfg.timeout)
        blocks = out.get("content") or []
        return "".join(b.get("text", "") for b in blocks if isinstance(b, dict)).strip()

    if cfg.provider == "ollama":
        url = f"{cfg.base_url}/api/chat"
        payload = {
            "model": cfg.model,
            "stream": False,
            "options": {"temperature": temperature},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        out = _post_json(url, payload, {}, cfg.timeout)
        return str((out.get("message") or {}).get("content", "")).strip()

    # openai / any OpenAI-compatible endpoint
    url = f"{cfg.base_url}/chat/completions"
    headers = {"Authorization": f"Bearer {cfg.api_key}"} if cfg.api_key else {}
    payload = {
        "model": cfg.model,
        "temperature": temperature,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    out = _post_json(url, payload, headers, cfg.timeout)
    choices = out.get("choices") or []
    if not choices:
        raise LLMError("LLM returned no choices")
    return str((choices[0].get("message") or {}).get("content", "")).strip()


# ---------------------------------------------------------------------------
# Structured style suggestion
# ---------------------------------------------------------------------------

_SYSTEM = """You are the style director for xLights holiday light displays.
Given a user's free-form prompt about a song or mood, propose a lighting style
as ONE JSON object, no prose, with exactly this shape:

{
  "palette": ["#RRGGBB", ... 3 to 8 hex colours],
  "effects": [
    {"role": "background", "effect": "<effect name>", "cadence": "section",
     "settings": {"<key>": "<value>"}, "why": "one short sentence"},
    {"role": "outline",    "effect": "<effect name>", "cadence": "beat",
     "settings": {}, "why": "..."},
    {"role": "accent",     "effect": "<effect name>", "cadence": "downbeat",
     "settings": {}, "why": "..."},
    {"role": "detail",     "effect": "<effect name>", "cadence": "section",
     "settings": {}, "why": "..."}
  ],
  "density": "sparse|medium|dense",
  "notes": "2-3 sentences explaining the look"
}

Rules:
- "effect" MUST be one of: %s
- Effect settings keys must look like xLights keys (e.g. "Marquee_Speed",
  "Twinkle_Count", "CHOICE_Butterfly_Colors"); omit unknown settings rather
  than inventing exotic ones.
- The four roles map to model classes: background = big fill models (trees,
  matrices), outline = house outlines/strips/arches, accent = stars/signs,
  detail = an extra layer on top during busy sections.
- Keep it musically motivated: match the cadence to the role.
""" % (", ".join(sorted(KNOWN_EFFECTS)))


def _extract_json(text: str) -> dict:
    text = text.strip()
    # Strip ```json fences if the model added them.
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    # Last resort: grab the outermost {...}.
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        try:
            obj = json.loads(text[start:end + 1])
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
    raise LLMError("LLM did not return a JSON object")


def suggest_with_llm(prompt: str, base: Style, cfg: LLMConfig) -> dict:
    """Ask the LLM for a style JSON for *prompt*, seeded with *base*."""
    user = (
        f"User prompt: {prompt or '(none)'}\n"
        f"Rule-based starting point: style '{base.id}' ({base.description})\n"
        f"Density: {base.density}. Palette: {', '.join(base.palette)}\n"
        "Respond with the JSON object only."
    )
    reply = chat(cfg, _SYSTEM, user)
    if not reply:
        raise LLMError("empty LLM reply")
    return _extract_json(reply)
