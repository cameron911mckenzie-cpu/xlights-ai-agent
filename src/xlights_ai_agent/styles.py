"""Show style library and prompt-driven style suggestions.

A :class:`Style` is a complete look for a sequence: an 8-slot colour palette
plus a set of *slots* -- effect recipes bound to a model role (background,
outline, accent, detail) and a beat cadence (section, bar, downbeat, beat,
half-beat). The planner turns slots into concrete effect placements.

``suggest_style`` matches a free-form prompt ("spooky halloween, lots of
strobes on the chorus") against the library using keywords, then applies
prompt tweaks: colour words rewrite the palette, mood words adjust density.
Optionally an LLM (``llm.py``) can produce the suggestion instead, with the
rule-based result as a validated fallback.
"""

from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

__all__ = [
    "EffectSlot",
    "Style",
    "StyleSuggestion",
    "list_styles",
    "get_style",
    "suggest_style",
    "merge_llm_suggestion",
    "KNOWN_EFFECTS",
    "ROLE_BACKGROUND",
    "ROLE_OUTLINE",
    "ROLE_ACCENT",
    "ROLE_DETAIL",
]

ROLE_BACKGROUND = "background"
ROLE_OUTLINE = "outline"
ROLE_ACCENT = "accent"
ROLE_DETAIL = "detail"

#: Every effect name xLights ships (src-core/effects/*Effect.cpp), used to
#: validate style/LLM suggestions before they reach the sequencer.
KNOWN_EFFECTS = frozenset("""
Adjust Bars Butterfly Candle Circles ColorWash Curtain DMX Duplicate Faces Fan
Fill Fire Fireworks Galaxy Garlands Glediator Guitar Kaleidoscope Life Lightning
Lines Liquid Marquee Meteors Morph MovingHead Music Off On Piano Pictures Pinwheel
Plasma Ripple Shockwave Shader Shape Shimmer SingleStrand Sketch Snowflakes
Snowstorm Spirals Spirograph State Strobe Tendril Text Tree Twinkle VUMeter Video
Warp Wave
""".split())

CADENCES = ("section", "bar", "downbeat", "beat", "half_beat")


@dataclass
class EffectSlot:
    """An effect recipe bound to a model role and a beat cadence."""

    role: str
    effect: str
    cadence: str
    settings: Dict[str, str] = field(default_factory=dict)
    palette_slots: Sequence[int] = (0, 1, 2, 3, 4, 5, 6, 7)  # which of the 8 colours are on
    layer: int = 0
    sections: Optional[Tuple[str, ...]] = None   # None = all sections; e.g. ("high",)
    span: float = 1.0                            # fraction of the cadence window used
    why: str = ""


@dataclass
class Style:
    """A complete show style."""

    id: str
    name: str
    description: str
    keywords: Tuple[str, ...]
    palette: List[str]                            # up to 8 hex colours
    slots: List[EffectSlot]
    density: str = "medium"                       # sparse | medium | dense
    mood: str = ""                                # free text used in suggestions
    notes: str = ""

    def derived(self, **changes) -> "Style":
        return dataclasses.replace(self, **changes)


# ---------------------------------------------------------------------------
# Colour helpers
# ---------------------------------------------------------------------------

_COLOR_WORDS: Dict[str, List[str]] = {
    "red": ["#FF1A1A", "#B30000", "#FF5C5C", "#800000", "#FF4D4D", "#CC0000", "#FF9999", "#660000"],
    "green": ["#1AFF5A", "#0E9E45", "#7CFFA8", "#006B2E", "#3DFF85", "#0B7A38", "#B8FFD1", "#034D20"],
    "blue": ["#1A6BFF", "#0B3FA8", "#79AFFF", "#003080", "#4D8CFF", "#1246B8", "#B3CCFF", "#001F66"],
    "white": ["#FFFFFF", "#F2F2F2", "#D9E6FF", "#C7C7C7", "#EAF2FF", "#FFFFFF", "#DCE9FF", "#B0B0B0"],
    "gold": ["#FFD700", "#E6B800", "#FFE766", "#B38600", "#FFDF4D", "#CC9F00", "#FFF2B3", "#806600"],
    "yellow": ["#FFE81A", "#E6C800", "#FFF380", "#B39F00", "#FFF14D", "#CCB800", "#FFFAC2", "#807300"],
    "orange": ["#FF8C1A", "#E06E00", "#FFB366", "#B35700", "#FFA34D", "#CC7400", "#FFD9B3", "#804200"],
    "purple": ["#A11AFF", "#7500CC", "#C780FF", "#4D0080", "#B84DFF", "#8C00E6", "#DFBBFF", "#330066"],
    "pink": ["#FF1A9E", "#CC007E", "#FF80C7", "#99005F", "#FF4DB8", "#E6008C", "#FFC2E1", "#660040"],
    "cyan": ["#1AE5FF", "#00B8CC", "#80F0FF", "#008099", "#4DEBFF", "#00CCE6", "#B3F7FF", "#005966"],
    "teal": ["#1AE5C3", "#00B89E", "#80F0E1", "#00806E", "#4DEBDB", "#00CCB8", "#B3F7F0", "#00594D"],
    "magenta": ["#FF1AE5", "#CC00B8", "#FF80F0", "#990080", "#FF4DEB", "#E600CC", "#FFC2F7", "#660059"],
    "brown": ["#8B4513", "#A0522D", "#CD853F", "#5C2E0E", "#B5651D", "#7B3F00", "#DEB887", "#40200A"],
    "black": ["#1A1A1A", "#000000", "#3D3D3D", "#0D0D0D", "#262626", "#141414", "#595959", "#000000"],
}

_MOOD_INTENSE = ("aggressive", "hard", "hardstyle", "metal", "rage", "intense", "angry",
                 "strobe", "strobes", "fast", "frenzy", "chaos", "drop", "drops", "edm")
_MOOD_SOFT = ("calm", "gentle", "soft", "slow", "peaceful", "elegant", "mellow",
              "serene", "smooth", "relaxing", "cozy", "subtle")


def _palette_from_words(words: List[str]) -> List[str]:
    """Cycle the requested colour families into an 8-slot palette."""
    out: List[str] = []
    for w in words:
        out.extend(_COLOR_WORDS[w])
    if not out:
        return []
    out = out[:8]
    while len(out) < 8:
        out.append(out[len(out) % max(1, len(words) * 8)])
    return out[:8]


def _find_color_words(prompt: str) -> List[str]:
    found = []
    for word in _COLOR_WORDS:
        if re.search(rf"\b{word}\b", prompt):
            found.append(word)
    return found


# ---------------------------------------------------------------------------
# The style library
# ---------------------------------------------------------------------------

def _build_library() -> Dict[str, Style]:
    styles: List[Style] = []

    styles.append(Style(
        id="christmas-classic",
        name="Christmas Classic",
        description="Traditional reds and greens with gentle snow, twinkles and "
                    "gold accents -- warm, family-friendly, always in taste.",
        keywords=("christmas", "holiday", "xmas", "santa", "traditional", "classic",
                  "traditional christmas", "winter christmas", "family", "warm"),
        palette=["#FF1A1A", "#1AFF5A", "#FFD700", "#FFFFFF", "#CC0000",
                 "#0E9E45", "#FFE766", "#F2F2F2"],
        density="medium",
        mood="warm and traditional",
        slots=[
            EffectSlot(ROLE_BACKGROUND, "Twinkle", "section",
                       settings={"Twinkle_Count": "30"}, palette_slots=(0, 1, 3, 4, 5, 7),
                       why="steady twinkle bed across each section"),
            EffectSlot(ROLE_OUTLINE, "Marquee", "bar",
                       settings={"Marquee_Speed": "30", "Marquee_Thickness": "20"},
                       palette_slots=(0, 2, 1),
                       why="chasing lights run round the house each bar"),
            EffectSlot(ROLE_ACCENT, "Shockwave", "downbeat",
                       settings={"Shockwave_Accel": "5", "Shockwave_Duration": "70"},
                       palette_slots=(2, 0, 3),
                       why="gold shockwave pops on every downbeat"),
            EffectSlot(ROLE_DETAIL, "Shimmer", "section",
                       settings={"Shimmer_Duty_Factor": "60"},
                       palette_slots=(2, 3, 7),
                       sections=("high",), layer=1,
                       why="shimmer layer only lifts the choruses"),
        ],
        notes="Pair with white lights for a snowy feel.",
    ))

    styles.append(Style(
        id="winter-wonderland",
        name="Winter Wonderland",
        description="Icy blues, silvers and snowfall with slow waves -- calm "
                    "verses that burst into sparkle on the choruses.",
        keywords=("winter", "ice", "icy", "snow", "snowy", "frost", "frozen",
                  "wonderland", "arctic", "cold", "nordic"),
        palette=["#1A6BFF", "#B3CCFF", "#FFFFFF", "#79AFFF", "#0B3FA8",
                 "#D9E6FF", "#C7C7C7", "#1AE5FF"],
        density="sparse",
        mood="cold, calm and sparkling",
        slots=[
            EffectSlot(ROLE_BACKGROUND, "Snowflakes", "section",
                       settings={"Snowflakes_Count": "35", "Snowflakes_Speed": "25"},
                       palette_slots=(2, 1, 5, 7),
                       why="falling snow fills the whole display per section"),
            EffectSlot(ROLE_OUTLINE, "Wave", "bar",
                       settings={"Number_Waves": "2", "Wave_Speed": "40"},
                       palette_slots=(0, 3, 1),
                       why="soft icy wave rolls along the outline each bar"),
            EffectSlot(ROLE_ACCENT, "Shockwave", "downbeat",
                       settings={"Shockwave_Accel": "3", "Shockwave_Cycles": "2"},
                       palette_slots=(7, 2, 0),
                       why="cold ring pulse on downbeats"),
            EffectSlot(ROLE_DETAIL, "Twinkle", "section",
                       settings={"Twinkle_Count": "45"},
                       palette_slots=(2, 1, 5),
                       sections=("high",), layer=1,
                       why="choruses glitter like fresh snow"),
        ],
    ))

    styles.append(Style(
        id="candy-cane-rush",
        name="Candy Cane Rush",
        description="Fast red-and-white stripes racing to every beat -- high "
                    "energy, sweet and relentless.",
        keywords=("candy", "candy cane", "rush", "fast", "speed", "race",
                  "stripes", "peppermint", "sugar"),
        palette=["#FF1A1A", "#FFFFFF", "#CC0000", "#FF9999", "#FF4D4D",
                 "#F2F2F2", "#B30000", "#E6E6E6"],
        density="dense",
        mood="fast and playful",
        slots=[
            EffectSlot(ROLE_OUTLINE, "Marquee", "beat",
                       settings={"Marquee_Speed": "60", "Marquee_Thickness": "35"},
                       palette_slots=(0, 1, 2, 5),
                       why="stripes chase every single beat"),
            EffectSlot(ROLE_BACKGROUND, "Bars", "bar",
                       settings={"Bars_Cycles": "4", "Bars_BarCount": "6"},
                       palette_slots=(0, 1, 2),
                       why="candy bars sweep the big models each bar"),
            EffectSlot(ROLE_ACCENT, "On", "downbeat",
                       palette_slots=(1, 0),
                       why="white flash on the downbeat"),
            EffectSlot(ROLE_DETAIL, "Shimmer", "section",
                       settings={"Shimmer_Duty_Factor": "75"},
                       palette_slots=(1, 4, 0),
                       sections=("high", "mid"), layer=1,
                       why="extra shimmer through the loud parts"),
        ],
    ))

    styles.append(Style(
        id="synthwave-retro",
        name="Synthwave Retro",
        description="80s neon: magenta, cyan and purple butterflies and grids "
                    "with retro strobe accents.",
        keywords=("synthwave", "retro", "80s", "eighties", "vaporwave", "outrun",
                  "neon", "arcade", "chiptune"),
        palette=["#FF1AE5", "#1AE5FF", "#A11AFF", "#FFFFFF", "#CC00B8",
                 "#00B8CC", "#7500CC", "#FF80F0"],
        density="medium",
        mood="nostalgic neon",
        slots=[
            EffectSlot(ROLE_BACKGROUND, "Butterfly", "section",
                       settings={"Butterfly_Speed": "35", "CHOICE_Butterfly_Colors": "Palette"},
                       palette_slots=(0, 1, 2, 5, 6, 7),
                       why="neon butterfly swirl across the section"),
            EffectSlot(ROLE_OUTLINE, "Circles", "beat",
                       settings={"Circles_Count": "3", "Circles_Speed": "60"},
                       palette_slots=(1, 0, 3),
                       why="expanding rings hit every beat"),
            EffectSlot(ROLE_ACCENT, "Pinwheel", "downbeat",
                       settings={"Pinwheel_Arms": "6", "Pinwheel_Speed": "55"},
                       palette_slots=(0, 2, 4),
                       why="pinwheel snaps on the downbeat"),
            EffectSlot(ROLE_DETAIL, "Strobe", "beat",
                       settings={"Number_Strobes": "3", "Strobe_Duration": "30"},
                       palette_slots=(1, 3),
                       sections=("high",), layer=1,
                       why="strobe flicker only on choruses"),
        ],
    ))

    styles.append(Style(
        id="electro-drop",
        name="Electro Drop",
        description="Club energy: strobes, shockwaves and fast circles locked "
                    "to the beat grid -- built for EDM drops.",
        keywords=("edm", "techno", "trance", "house", "dubstep", "drop", "club",
                  "dance", "electro", "festival", "bass", "rave", "party"),
        palette=["#FFFFFF", "#1AE5FF", "#FF1A1A", "#1A6BFF", "#C7C7C7",
                 "#00B8CC", "#FF4D4D", "#79AFFF"],
        density="dense",
        mood="relentless club energy",
        slots=[
            EffectSlot(ROLE_OUTLINE, "Circles", "beat",
                       settings={"Circles_Count": "5", "Circles_Speed": "80"},
                       palette_slots=(0, 1, 4),
                       why="rings locked to every beat"),
            EffectSlot(ROLE_ACCENT, "Shockwave", "downbeat",
                       settings={"Shockwave_Accel": "8", "Shockwave_Duration": "50"},
                       palette_slots=(2, 0, 1),
                       why="big shockwave on each bar start"),
            EffectSlot(ROLE_BACKGROUND, "ColorWash", "section",
                       settings={"ColorWash_Cycles": "2"},
                       palette_slots=(1, 3, 0, 5),
                       why="colour wash keeps the base alive through sections"),
            EffectSlot(ROLE_DETAIL, "Strobe", "half_beat",
                       settings={"Number_Strobes": "5", "Strobe_Duration": "20"},
                       palette_slots=(0, 4),
                       sections=("high",), layer=1,
                       why="half-beat strobes through the drops"),
            EffectSlot(ROLE_DETAIL, "Twinkle", "section",
                       settings={"Twinkle_Count": "25"},
                       palette_slots=(1, 4),
                       sections=("low", "mid"), layer=1,
                       why="keeps quieter sections breathing"),
        ],
    ))

    styles.append(Style(
        id="spooky-halloween",
        name="Spooky Halloween",
        description="Orange, purple and toxic green with creeping meteors and "
                    "lightning-flash accents.",
        keywords=("halloween", "spooky", "scary", "horror", "zombie", "ghost",
                  "haunted", "pumpkin", "creepy", "nightmare"),
        palette=["#FF8C1A", "#A11AFF", "#1AFF5A", "#1A1A1A", "#E66A00",
                 "#7500CC", "#0E9E45", "#FFB366"],
        density="medium",
        mood="eerie and fun",
        slots=[
            EffectSlot(ROLE_BACKGROUND, "Meteors", "section",
                       settings={"Meteors_Count": "6", "Meteors_Speed": "35"},
                       palette_slots=(2, 5, 3),
                       why="ghostly meteors drift through each section"),
            EffectSlot(ROLE_OUTLINE, "Circles", "bar",
                       settings={"Circles_Count": "4", "Circles_Speed": "40"},
                       palette_slots=(1, 0, 3),
                       why="creeping rings along the outline"),
            EffectSlot(ROLE_ACCENT, "Shockwave", "downbeat",
                       settings={"Shockwave_Accel": "2", "Shockwave_Cycles": "3"},
                       palette_slots=(0, 2, 4),
                       why="pumpkin-orange burst on downbeats"),
            EffectSlot(ROLE_DETAIL, "Twinkle", "beat",
                       settings={"Twinkle_Count": "20"},
                       palette_slots=(1, 2, 7),
                       sections=("high",), layer=1,
                       why="nervous flicker when it gets loud"),
        ],
    ))

    styles.append(Style(
        id="rainbow-pop",
        name="Rainbow Pop",
        description="Bright saturated rainbow -- butterflies, marquees and "
                    "shimmer for feel-good pop songs.",
        keywords=("rainbow", "pop", "upbeat", "colorful", "colourful", "bright",
                  "fun", "happy", "kids", "summer"),
        palette=["#FF1A1A", "#FF8C1A", "#FFE81A", "#1AFF5A", "#1AE5FF",
                 "#1A6BFF", "#A11AFF", "#FF1AE5"],
        density="dense",
        mood="cheerful and colourful",
        slots=[
            EffectSlot(ROLE_BACKGROUND, "Butterfly", "section",
                       settings={"Butterfly_Speed": "45", "CHOICE_Butterfly_Colors": "Rainbow"},
                       palette_slots=(0, 1, 2, 3, 4, 5, 6, 7),
                       why="rainbow butterfly floods the big models"),
            EffectSlot(ROLE_OUTLINE, "Marquee", "beat",
                       settings={"Marquee_Speed": "45", "Marquee_Thickness": "25"},
                       palette_slots=(0, 2, 4, 6),
                       why="colour chase on every beat"),
            EffectSlot(ROLE_ACCENT, "Pinwheel", "downbeat",
                       settings={"Pinwheel_Arms": "8", "Pinwheel_Speed": "65"},
                       palette_slots=(3, 5, 7),
                       why="pinwheel burst per bar"),
            EffectSlot(ROLE_DETAIL, "Shimmer", "section",
                       settings={"Shimmer_Duty_Factor": "70"},
                       palette_slots=(2, 3, 5, 7),
                       layer=1,
                       why="constant sparkle layer"),
        ],
    ))

    styles.append(Style(
        id="elegant-gold",
        name="Elegant Gold",
        description="Champagne, amber and cream -- slow washes, delicate "
                    "garlands and restrained sparkle for formal displays.",
        keywords=("elegant", "gold", "classy", "formal", "champagne", "luxe",
                  "luxury", "sophisticated", "warm glow", "new year", "gala"),
        palette=["#FFD700", "#FFE766", "#FFFFFF", "#E6B800", "#B38600",
                 "#F2F2F2", "#FFDF4D", "#CC9F00"],
        density="sparse",
        mood="refined and warm",
        slots=[
            EffectSlot(ROLE_BACKGROUND, "ColorWash", "section",
                       settings={"ColorWash_Cycles": "1"},
                       palette_slots=(0, 1, 4, 6),
                       why="slow golden wash across sections"),
            EffectSlot(ROLE_OUTLINE, "Garlands", "bar",
                       settings={"Garlands_Spacing": "25", "Garlands_Cycles": "2"},
                       palette_slots=(0, 1, 2),
                       why="draped garlands per bar"),
            EffectSlot(ROLE_ACCENT, "Shockwave", "downbeat",
                       settings={"Shockwave_Accel": "2", "Shockwave_Cycles": "2"},
                       palette_slots=(1, 0, 2),
                       why="soft champagne ring on downbeats"),
            EffectSlot(ROLE_DETAIL, "Meteors", "section",
                       settings={"Meteors_Count": "4", "Meteors_Speed": "20"},
                       palette_slots=(1, 6, 2),
                       sections=("high",), layer=1,
                       why="gentle gold streaks when the music swells"),
        ],
    ))

    styles.append(Style(
        id="meteor-storm",
        name="Meteor Storm",
        description="Deep space blues with heavy meteor showers, spirals and "
                    "starfield twinkle -- cinematic and wide.",
        keywords=("space", "meteor", "meteor shower", "galaxy", "sci-fi", "scifi",
                  "star", "stars", "cosmic", "universe", "orbital", "sky"),
        palette=["#0B3FA8", "#FFFFFF", "#1A6BFF", "#1AE5FF", "#003080",
                 "#C7C7C7", "#79AFFF", "#001F66"],
        density="medium",
        mood="cinematic and wide",
        slots=[
            EffectSlot(ROLE_BACKGROUND, "Meteors", "section",
                       settings={"Meteors_Count": "10", "Meteors_Speed": "55"},
                       palette_slots=(1, 2, 3, 7),
                       why="meteor storm over every section"),
            EffectSlot(ROLE_OUTLINE, "Circles", "bar",
                       settings={"Circles_Count": "6", "Circles_Speed": "55"},
                       palette_slots=(3, 1, 0),
                       why="sonar rings each bar"),
            EffectSlot(ROLE_ACCENT, "On", "downbeat",
                       palette_slots=(1, 3),
                       why="star flash on downbeats"),
            EffectSlot(ROLE_DETAIL, "Twinkle", "section",
                       settings={"Twinkle_Count": "60"},
                       palette_slots=(1, 5, 6),
                       sections=("high", "mid"), layer=1,
                       why="dense starfield in the busy parts"),
        ],
    ))

    styles.append(Style(
        id="classic-chase",
        name="Classic Chase",
        description="The dependable all-rounder: multi-colour chases on the "
                    "beats with a twinkle bed -- works on any song, any layout.",
        keywords=("chase", "all rounder", "all-rounder", "generic", "simple",
                  "easy", "safe", "outline", "corsa"),
        palette=["#FF1A1A", "#FFE81A", "#1AFF5A", "#1A6BFF", "#FFFFFF",
                 "#FF1AE5", "#1AE5FF", "#FF8C1A"],
        density="medium",
        mood="straightforward and lively",
        slots=[
            EffectSlot(ROLE_OUTLINE, "Marquee", "beat",
                       settings={"Marquee_Speed": "40", "Marquee_Thickness": "30"},
                       palette_slots=(0, 1, 2, 3, 4),
                       why="workhorse chase locked to the beat"),
            EffectSlot(ROLE_BACKGROUND, "Twinkle", "section",
                       settings={"Twinkle_Count": "40"},
                       palette_slots=(0, 2, 3, 4),
                       why="twinkle bed under everything"),
            EffectSlot(ROLE_ACCENT, "On", "downbeat",
                       palette_slots=(4, 1),
                       why="clean flash marks the downbeat"),
            EffectSlot(ROLE_DETAIL, "Shimmer", "section",
                       settings={"Shimmer_Duty_Factor": "65"},
                       palette_slots=(5, 6, 4),
                       sections=("high",), layer=1,
                       why="lift on the choruses"),
        ],
    ))

    lib = {s.id: s for s in styles}
    return lib


STYLES: Dict[str, Style] = _build_library()


def list_styles() -> List[Style]:
    """All bundled styles, in a stable order."""
    return list(STYLES.values())


def get_style(style_id: str) -> Style:
    if style_id not in STYLES:
        known = ", ".join(STYLES)
        raise KeyError(f"Unknown style '{style_id}'. Known styles: {known}")
    # Return a deep-ish copy so callers can tweak without mutating the library.
    return _clone(STYLES[style_id])


def _clone(style: Style) -> Style:
    return dataclasses.replace(
        style,
        palette=list(style.palette),
        slots=[dataclasses.replace(s, settings=dict(s.settings),
                                   palette_slots=tuple(s.palette_slots),
                                   sections=tuple(s.sections) if s.sections else None)
               for s in style.slots],
    )


# ---------------------------------------------------------------------------
# Prompt -> style
# ---------------------------------------------------------------------------

@dataclass
class StyleSuggestion:
    """The answer to "what style should this song be?"."""

    style: Style
    base_id: str
    reason: str
    matched_keywords: List[str] = field(default_factory=list)
    tweaks: List[str] = field(default_factory=list)
    source: str = "rules"     # "rules" | "llm"

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "base_style": self.base_id,
            "style": {
                "id": self.style.id,
                "name": self.style.name,
                "description": self.style.description,
                "density": self.style.density,
                "mood": self.style.mood,
                "notes": self.style.notes,
                "palette": list(self.style.palette),
                "slots": [
                    {
                        "role": s.role,
                        "effect": s.effect,
                        "cadence": s.cadence,
                        "layer": s.layer,
                        "settings": dict(s.settings),
                        "sections": list(s.sections) if s.sections else None,
                        "why": s.why,
                    }
                    for s in self.style.slots
                ],
            },
            "reason": self.reason,
            "matched_keywords": self.matched_keywords,
            "tweaks": self.tweaks,
        }


def _score_style(prompt: str, style: Style) -> Tuple[int, List[str]]:
    p = " " + re.sub(r"[^a-z0-9 ]+", " ", prompt.lower()) + " "
    score, matched = 0, []
    for kw in style.keywords:
        if f" {kw} " in p or p.strip() == kw:
            score += 3 if len(kw.split()) > 1 else 2
            matched.append(kw)
    # Generic fallback words for the all-rounder.
    if style.id == "classic-chase" and not matched:
        score += 1
    return score, matched


def suggest_style(prompt: str, explicit_style: Optional[str] = None,
                  use_llm: bool = False, llm_config=None) -> StyleSuggestion:
    """Suggest a :class:`Style` for *prompt*.

    * If *explicit_style* is given, that style wins (still prompt-tweaked).
    * Keyword scoring picks the closest library style.
    * Colour words in the prompt rewrite the palette; mood words tune density.
    * With *use_llm* and a configured :class:`llm.LLMConfig`, an LLM refines the
      suggestion and the rules result is used to validate/fall back.
    """
    tweaks: List[str] = []

    if explicit_style:
        base = get_style(explicit_style)
        matched = list(base.keywords[:3])
        reason = f"style '{explicit_style}' explicitly selected"
    else:
        prompt = prompt or ""
        best, best_score, matched = None, -1, []
        for style in STYLES.values():
            s, kw = _score_style(prompt, style)
            # Deterministic tie-break by library order.
            if s > best_score:
                best, best_score, matched = style, s, kw
        assert best is not None
        base = _clone(best)
        if best_score > 0:
            reason = (f"keywords {', '.join(repr(k) for k in matched)} match "
                      f"style '{best.id}'")
        else:
            reason = (f"no strong keyword match -- defaulting to the all-rounder "
                      f"'{best.id}'")

    base = _clone(base)

    # --- prompt tweaks -----------------------------------------------------
    color_words = _find_color_words(prompt or "")
    if color_words and not explicit_style:
        new_pal = _palette_from_words(color_words)
        if new_pal:
            base = base.derived(palette=new_pal)
            tweaks.append(f"palette overridden to {'/'.join(color_words)}")

    low = (prompt or "").lower()
    intense_hit = [w for w in _MOOD_INTENSE if re.search(rf"\b{w}\b", low)]
    soft_hit = [w for w in _MOOD_SOFT if re.search(rf"\b{w}\b", low)]
    if intense_hit and base.density != "dense":
        base = base.derived(density="dense")
        tweaks.append(f"density raised to dense ({intense_hit[0]})")
    elif soft_hit and base.density != "sparse":
        base = base.derived(density="sparse")
        tweaks.append(f"density lowered to sparse ({soft_hit[0]})")

    # --- optional LLM refinement ------------------------------------------
    if use_llm:
        from . import llm as llm_mod  # local import: keep core deps light
        cfg = llm_config or llm_mod.LLMConfig.from_env()
        if cfg and cfg.is_configured():
            try:
                data = llm_mod.suggest_with_llm(prompt, base, cfg)
                merged = merge_llm_suggestion(base, data)
                if merged is not None:
                    merged_style, llm_tweaks = merged
                    tweaks.extend(f"llm: {t}" for t in llm_tweaks)
                    base = merged_style
                    return StyleSuggestion(
                        style=base, base_id=base.id, reason=base.notes or reason,
                        matched_keywords=matched, tweaks=tweaks, source="llm")
            except Exception as exc:  # noqa: BLE001 - any LLM failure falls back
                tweaks.append(f"llm unavailable ({exc}); used rule-based suggestion")

    return StyleSuggestion(style=base, base_id=base.id, reason=reason,
                           matched_keywords=matched, tweaks=tweaks, source="rules")


def merge_llm_suggestion(base: Style, data: Optional[dict]
                         ) -> Optional[Tuple[Style, List[str]]]:
    """Validate and merge an LLM's style JSON onto *base*.

    Returns ``(merged_style, tweaks)`` or ``None`` when the LLM output carries
    nothing usable -- callers then keep the rule-based suggestion.
    """
    if not isinstance(data, dict):
        return None
    style = _clone(base)
    tweaks: List[str] = []

    pal = data.get("palette")
    if isinstance(pal, list):
        colors = []
        for c in pal:
            if isinstance(c, dict):
                c = c.get("hex") or c.get("hexValue") or ""
            if isinstance(c, str) and re.fullmatch(r"#?[0-9a-fA-F]{6}", c):
                colors.append(c if c.startswith("#") else "#" + c)
        if len(colors) >= 3:
            while len(colors) < 8:
                colors.append(colors[len(colors) % len(pal)] if pal else colors[-1])
            style = style.derived(palette=colors[:8])
            tweaks.append("palette taken from LLM suggestion")

    effs = data.get("effects")
    if isinstance(effs, list):
        by_role: Dict[str, dict] = {}
        for e in effs:
            if not isinstance(e, dict):
                continue
            role = e.get("role")
            name = e.get("effect") or e.get("name")
            if role in (ROLE_BACKGROUND, ROLE_OUTLINE, ROLE_ACCENT, ROLE_DETAIL) \
                    and isinstance(name, str) and name in KNOWN_EFFECTS:
                by_role[role] = e
        if by_role:
            new_slots = []
            replaced = set()
            for slot in style.slots:
                e = by_role.get(slot.role)
                if e and slot.role not in replaced:
                    settings = e.get("settings")
                    clean = {str(k): str(v) for k, v in settings.items()} \
                        if isinstance(settings, dict) else dict(slot.settings)
                    new_slots.append(dataclasses.replace(
                        slot, effect=e["effect"], settings=clean,
                        why=f"llm: {e.get('why') or slot.why}"))
                    replaced.add(slot.role)
                else:
                    new_slots.append(slot)
            # Add any brand-new roles the LLM suggested (not already covered).
            for role, e in by_role.items():
                if role in replaced:
                    continue
                cadence = e.get("cadence")
                if cadence not in CADENCES:
                    cadence = {ROLE_BACKGROUND: "section", ROLE_OUTLINE: "beat",
                               ROLE_ACCENT: "downbeat", ROLE_DETAIL: "section"}[role]
                settings = e.get("settings")
                new_slots.append(EffectSlot(
                    role=role, effect=e["effect"], cadence=cadence,
                    settings={str(k): str(v) for k, v in settings.items()}
                    if isinstance(settings, dict) else {},
                    layer=1 if role == ROLE_DETAIL else 0,
                    why=f"llm: {e.get('why') or 'suggested by LLM'}"))
                replaced.add(role)
            style = style.derived(slots=new_slots)
            tweaks.append(f"effect overrides applied for {', '.join(sorted(replaced))}")

    dens = data.get("density")
    if dens in ("sparse", "medium", "dense") and dens != style.density:
        style = style.derived(density=dens)
        tweaks.append(f"density set to {dens} by LLM")

    note = data.get("notes") or data.get("reason")
    if isinstance(note, str) and note.strip():
        style = style.derived(notes=note.strip()[:400])
        tweaks.append("notes added")

    if not tweaks:
        return None
    style = style.derived(id=f"{base.id}-llm", name=f"{base.name} (LLM)")
    return style, tweaks
