"""The coursework interface.

Subclass :class:`TeamController` and implement ``act``. One method decides for
the whole team on every tick, whether you have the ball or not — reading the
match and working out what kind of situation you are in is part of the problem,
not something the engine does on your behalf.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Sequence, Tuple

from .actions import PlayerAction, TeamAction
from .observation import Observation

#: One ``(x, y)`` per player, in slot order. What ``initial_formation`` returns.
Formation = Sequence[Tuple[float, float]]


class TeamController:
    """Base class for a team.

    Override ``act``. Optionally override ``reset`` to clear per-match state,
    and ``name``/``version`` so results identify your team.

    ``act`` must return a :class:`~soccer.actions.TeamAction` covering the
    players you want to move. Any player you leave out simply does nothing that
    tick — a missing action is never treated as an error.
    """

    #: Shown in results and replays. Override in your submission.
    name: str = "unnamed_team"
    #: Bump when you change behaviour; it is recorded with every result.
    version: str = "1"

    def act(self, observation: Observation) -> TeamAction:
        """Called once per tick. Decide what every one of your players does.

        The same method runs whether the ball is yours or theirs. Establishing
        which of those it is — from ``observation.ball.controlling_team``, from
        :func:`soccer.closest_to_ball`, or from any reading of your own — is the
        first thing most teams do here.
        """
        raise NotImplementedError(
            f"{type(self).__name__} must implement act(self, observation)"
        )

    def initial_formation(self, field) -> Optional[Formation]:
        """Where your team stands at a kickoff. Optional.

        Return one ``(x, y)`` per player in slot order — slot 0 is your
        goalkeeper — in the usual canonical frame: your goal at
        ``field.my_goal``, theirs at ``field.opponent_goal``, forward ``+x``.
        Return ``None``, the default, to take the engine's formation.

        Three rules apply, and a spot that breaks one is **moved to the nearest
        spot that does not** rather than rejected, so this cannot fail:

        * inside the pitch, by one player radius;
        * on your own half, ``x <= 0``;
        * outside the centre circle, ``field.centre_circle_radius`` from the
          centre spot — nobody may line up on top of a dead ball.

        Called once per match, before :meth:`reset`, and used at every kickoff
        in it, including restarts after a goal. ``soccer validate`` reports any
        spot that had to be moved.
        """
        return None

    def reset(self, seed: int) -> None:
        """Called once before each match. Clear any per-match state here."""

    def on_match_end(self, result: Dict[str, Any]) -> None:
        """Called once after each match, with the result record."""

    # -- convenience ------------------------------------------------------

    @staticmethod
    def idle(observation: Observation) -> TeamAction:
        """A complete no-op action for every player. A useful fallback."""
        action = TeamAction()
        for player in observation.my_players:
            action.set(player.id, PlayerAction.noop())
        return action

    def describe(self) -> str:
        return f"{self.name} v{self.version} ({type(self).__name__})"


class FunctionController(TeamController):
    """Wraps a plain function as a controller. Handy in tests and notebooks."""

    def __init__(
        self,
        act_fn,
        name: str = "function_team",
        version: str = "1",
    ) -> None:
        self._act = act_fn
        self.name = name
        self.version = version

    def act(self, observation: Observation) -> TeamAction:
        return self._act(observation)
