"""xLights AI Auto Sequencer.

An addon for [xLights](https://www.xlights.org) that automatically sequences
songs from their music beats and suggests show styles from free-form prompts.

Public entry points:
    * ``analyze_beats`` / ``decode_audio``  -- music beat analysis
    * ``suggest_style``                    -- prompt -> style suggestion
    * ``build_plan``                       -- beats + models + style -> effect plan
    * ``XLightsClient``                    -- drive a running xLights instance
    * ``run_pipeline``                     -- the full auto-sequencing pipeline
"""

__version__ = "0.1.0"

from .audio import AudioData, Beat, BeatAnalysis, Section, analyze_beats, decode_audio
from .planner import Placement, SequencePlan, build_plan
from .sequencer import SequenceRequest, SequenceResult, run_pipeline
from .styles import Style, StyleSuggestion, get_style, list_styles, suggest_style
from .xlights_client import XLightsClient, XLightsConnection, XLightsError, discover

__all__ = [
    "__version__",
    "AudioData",
    "Beat",
    "BeatAnalysis",
    "Section",
    "decode_audio",
    "analyze_beats",
    "Style",
    "StyleSuggestion",
    "get_style",
    "list_styles",
    "suggest_style",
    "Placement",
    "SequencePlan",
    "build_plan",
    "XLightsClient",
    "XLightsConnection",
    "XLightsError",
    "discover",
    "SequenceRequest",
    "SequenceResult",
    "run_pipeline",
]
