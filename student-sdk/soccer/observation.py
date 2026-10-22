"""The observation handed to a controller each tick.

This is a re-export of the engine's native observation type, documented here
because it is the main thing a student reads.

**You never need to know which end of the pitch you are playing.** Every
observation arrives in the same canonical frame, whichever side the engine
assigned you:

* the goal you defend is at ``(-width / 2, 0)``, the goal you attack is at
  ``(+width / 2, 0)``, and the centre spot is ``(0, 0)``;
* **forward is always ``+x``**, so ``attack_direction`` is always ``+1.0``;
* your players are ids ``0 .. n-1`` in slot order, the opposition ``n .. 2n-1``;
* ``team`` reads ``0`` for you and ``1`` for them, everywhere it appears;
* ``score`` is ``(mine, theirs)``.

The actions you return are read in the same frame: a movement of ``(1, 0)``
means "run at the goal I am attacking", and the engine rotates it onto the
pitch for you. Write one team, not one per side.

Internally the engine still plays on a real pitch where one side defends ``-x``
and the other ``+x``; the mapping is applied as the observation is built and
undone as your action is validated. ``observation.side`` reports which side you
were actually given — for logging and for lining a run up against a replay.
Branching on it puts back exactly the problem this frame removes.

Attributes
----------
tick, max_ticks, time_remaining
    Where the match is. ``time_remaining`` is a fraction in ``[0, 1]``.
phase
    ``"ATTACK"`` or ``"DEFEND"``: the engine's own reading of possession,
    reported for information. Nothing dispatches on it — your ``act`` runs
    either way, and deciding what kind of situation you are in is yours.
score
    ``(my_goals, their_goals)``.
team, side
    ``team`` is always ``0``. ``side`` is the absolute side you drew, ``0`` or
    ``1``, and is informational only.
my_players, opponents
    Lists of players with ``id``, ``position``, ``velocity``, ``facing``,
    ``kick_cooldown_ticks`` and ``has_control``. ``my_players`` is in slot
    order, so ``my_players[0]`` is your goalkeeper and its ``id`` is ``0``.
ball
    ``position``, ``velocity``, ``controlling_player``, ``controlling_team``
    and ``last_touch``. The ball stays on the plane, so its position is all
    there is to it.
field
    Dimensions, ``my_goal``, ``opponent_goal``, ``attack_direction`` and the
    radii, speeds and kick range the physics uses. It also publishes the ball's
    own physics — ``simulation_hz``, ``ball_friction``, ``ball_air_friction``,
    ``gravity``, ``bounce_restitution``, ``bounce_friction`` and
    ``min_bounce_speed`` — which is everything you need to roll the ball
    forward yourself and land where the engine will::

        def predict_ball(obs, ticks):
            f = obs.field
            dt = 1.0 / f.simulation_hz
            x, y = obs.ball.position
            vx, vy = obs.ball.velocity
            for _ in range(ticks):
                x, y = x + vx * dt, y + vy * dt
                vx, vy = vx * f.ball_friction, vy * f.ball_friction
            return x, y

events
    What happened since your previous decision, as dictionaries with a
    ``kind`` key: kicks, turnovers, goals, policy errors and timeouts. Player
    ids, team ids and directions in an event are in the canonical frame too.

Methods
-------
closest_my_player_to(point) / closest_opponent_to(point)
    Nearest player, ties broken on the lower id.
my_players_by_distance_to(point)
    Your players sorted nearest-first.
can_kick(player_id)
    True when that player is in range of the ball and off cooldown.
ticks_to_cover(distance, power)
    How long a kick of that power takes to roll that far, under drag.
simulation_hz()
    Ticks per second, for turning any of the above into seconds.
normalise(point) / denormalise(point)
    The same coordinates scaled to ``[-1, 1]`` on both axes.

That is the whole list, and it is deliberately short. Where the ball is
heading, where to meet it, where to stand, who to mark and whether a pass is on
are not facts about the match — they are the decisions your team exists to
make, so the engine does not make them for you. It gives you the positions, the
velocities and the physics; the football is yours to write.
"""

from __future__ import annotations

from ._engine import BallView, FieldView, Observation, PlayerView

__all__ = ["Observation", "PlayerView", "BallView", "FieldView"]
