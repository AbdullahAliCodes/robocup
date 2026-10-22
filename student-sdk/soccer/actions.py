"""Action types returned by a controller.

The engine clamps and validates everything it receives, so these classes are a
convenience rather than a safety barrier: an oversized movement vector is
scaled down, a NaN is rejected, and a missing player gets a no-op. What the
types buy you is a clear shape and an early, readable error on your own
machine.

Everything here is in the canonical frame your observation arrived in: ``+x``
is the way you are attacking, and player ids are ``0 .. n-1`` whichever side
you drew. Addressing an opponent's id is reported back to you as an invalid
player id rather than applied to one of your own.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional

from .geometry import Vec2, VectorLike, _pair


@dataclass(frozen=True)
class PlayerAction:
    """One player's intent for one tick.

    ``movement`` is a desired direction; its magnitude doubles as a throttle in
    ``[0, 1]`` and anything longer is scaled down by the engine. A kick is only
    applied when the player is within ``kick_range`` of the ball and off
    cooldown; otherwise it is rejected and counted.
    """

    movement: VectorLike = (0.0, 0.0)
    kick_direction: Optional[VectorLike] = None
    kick_power: float = 0.0

    @staticmethod
    def noop() -> "PlayerAction":
        return PlayerAction()

    @staticmethod
    def move(movement: VectorLike) -> "PlayerAction":
        return PlayerAction(movement=movement)

    @staticmethod
    def kick(
        kick_direction: VectorLike,
        kick_power: float = 1.0,
        movement: VectorLike = (0.0, 0.0),
    ) -> "PlayerAction":
        """A kick: a direction and a power in ``[0, 1]``, optionally while running.

        The ball stays on the turf, so a kick is a direction and a power and
        nothing else. There is no height to aim at and no elevation to choose.
        """
        return PlayerAction(
            movement=movement,
            kick_direction=kick_direction,
            kick_power=kick_power,
        )

    def as_dict(self) -> Dict[str, Any]:
        """The plain form the engine consumes."""
        payload: Dict[str, Any] = {"movement": tuple(_pair(self.movement))}
        if self.kick_direction is not None:
            payload["kick_direction"] = tuple(_pair(self.kick_direction))
            payload["kick_power"] = float(self.kick_power)
        return payload


@dataclass
class TeamAction:
    """Actions for every player on one team, plus optional viewer annotations.

    ``debug`` is non-authoritative. The simulator ignores it entirely; only the
    viewer reads it. It is size-limited and dropped if it grows too large, and
    it can never change physics, scoring or observations.

    Annotations are in your canonical frame as well: give the coordinates and
    the player ids you were working with and the viewer draws them at the right
    end of the pitch.

    Recognised debug keys (all optional)::

        roles    {player_id: "label"}
        targets  {player_id: (x, y)}
        passes   [{"from": (x, y), "to": (x, y)}]
        marks    [{"from": (x, y), "to": (x, y)}]
        text     "free-form status line"
    """

    players: Dict[int, PlayerAction] = field(default_factory=dict)
    debug: Optional[Dict[str, Any]] = None

    def __init__(
        self,
        players: Optional[Mapping[int, PlayerAction]] = None,
        debug: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.players = dict(players or {})
        self.debug = debug

    def set(self, player_id: int, action: PlayerAction) -> "TeamAction":
        self.players[int(player_id)] = action
        return self

    def move(self, player_id: int, movement: VectorLike) -> "TeamAction":
        return self.set(player_id, PlayerAction.move(movement))

    def kick(
        self,
        player_id: int,
        kick_direction: VectorLike,
        kick_power: float = 1.0,
        movement: VectorLike = (0.0, 0.0),
    ) -> "TeamAction":
        return self.set(
            player_id, PlayerAction.kick(kick_direction, kick_power, movement)
        )

    def annotate(self, **entries: Any) -> "TeamAction":
        """Adds viewer annotations. Purely cosmetic."""
        if self.debug is None:
            self.debug = {}
        self.debug.update(entries)
        return self

    # There is deliberately no `as_dict` here. The engine reads a team action
    # either as a bare `{player_id: action}` mapping or through `.players` and
    # `.debug`, and a wrapper dict of `{"players": ...}` would be misread as a
    # mapping whose key is the string "players". Pass the object itself.

    def __len__(self) -> int:
        return len(self.players)

    def __contains__(self, player_id: object) -> bool:
        return player_id in self.players
