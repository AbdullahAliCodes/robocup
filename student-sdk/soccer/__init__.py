"""Student SDK for the 2D soccer agent simulation platform.

Write a team by subclassing :class:`TeamController` and implementing ``act``,
which is called once per tick and decides for every one of your players:

    from soccer import TeamController, PlayerAction, TeamAction, direction

    class MyTeam(TeamController):
        def act(self, obs):
            ...

Then play it::

    python -m soccer.cli play my_team.py:MyTeam --against balanced

Everything authoritative — physics, rules, possession, validation, timeouts —
lives in the compiled engine. This package is a convenience layer over it, so
a match run here obeys exactly the rules the course server applies.
"""

from __future__ import annotations

# The compiled engine first: everything else in the package depends on it, and
# a missing extension should produce a build instruction rather than a cascade
# of confusing import errors from the modules that use it.
try:
    from . import _engine
except ImportError as exc:  # pragma: no cover - only when the wheel is unbuilt
    raise ImportError(
        "The compiled simulation engine (soccer._engine) is not importable.\n"
        "Build it with:  maturin develop --release\n"
        "or install the published wheel for your platform."
    ) from exc

from ._engine import ENGINE_VERSION, load_config, read_replay
from .actions import PlayerAction, TeamAction
from .controller import TeamController
from .env import SoccerEnv
from .errors import PolicyError, SoccerError, SubmissionError
from .geometry import (
    Vec2,
    clamp,
    closest_point_on_segment,
    direction,
    distance,
    lerp,
    normalise,
    scale_to_length,
)
from .helpers import closest_to_ball, load_controller
from .observation import Observation
from .runner import MatchOutcome, baseline_names, evaluate, play_match

__version__ = ENGINE_VERSION

ATTACK = "ATTACK"
DEFEND = "DEFEND"

__all__ = [
    "ATTACK",
    "DEFEND",
    "ENGINE_VERSION",
    "MatchOutcome",
    "Observation",
    "PlayerAction",
    "PolicyError",
    "SoccerEnv",
    "SoccerError",
    "SubmissionError",
    "TeamAction",
    "TeamController",
    "Vec2",
    "__version__",
    "baseline_names",
    "clamp",
    "closest_point_on_segment",
    "closest_to_ball",
    "direction",
    "distance",
    "evaluate",
    "lerp",
    "load_config",
    "load_controller",
    "normalise",
    "play_match",
    "read_replay",
    "scale_to_length",
]
