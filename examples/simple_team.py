"""The starting point for a submission.

About as simple as a team can be while still playing football. Every player
asks two questions and does one of four things:

    is the ball ours?
        am I the closest of us to it?   ->  shoot at their goal
        otherwise                       ->  push up towards their goal
    otherwise
        am I the closest of us to it?   ->  go and get it
        otherwise                       ->  drop back towards our goal

That is the whole team. It will lose to the reference opponents, which is the
point — it is something to improve, not something to copy. Nothing here is
hidden in a helper: the engine tells you where everybody is, and turning that
into football is the assignment.

Obvious things wrong with it, in roughly the order they cost you goals: nobody
keeps goal, the four players who are not chasing all run to the same place,
nobody ever passes, and every shot is struck at full power from wherever the
player happens to be standing.

There is one method, ``act``, called on every tick of the match. It decides
whose ball it is first, which is what every team ends up doing in some form.

Run it::

    python -m soccer.cli play examples/simple_team.py --against balanced
    python -m soccer.cli play examples/simple_team.py --against balanced --render human
"""

from soccer import PlayerAction, TeamAction, TeamController, direction


class SimpleTeam(TeamController):
    name = "simple_team"
    version = "1"

    def initial_formation(self, field):
        """Where we stand at a kickoff. Optional — delete it for the default.

        Slot order, slot 0 the keeper, in the usual frame: our goal on the left
        at ``-x``, theirs on the right. A spot outside the rules is moved to the
        nearest one inside them, so nothing here can be illegal — but a spot
        inside the centre circle will not stay where you put it.
        """
        keeper_x = field.my_goal[0] + field.player_radius * 2
        # Just outside the centre circle: as close to the ball as the rules
        # allow, which is where the kickoff is won or lost.
        forward_x = -field.centre_circle_radius - 1.0
        return [
            (keeper_x, 0.0),          # keeper
            (-20.0, -12.0),           # left back
            (-20.0, 12.0),            # right back
            (forward_x, -6.0),        # left forward
            (forward_x, 6.0),         # right forward
        ]

    def act(self, obs):
        """Called on every tick. One action for every one of your players.

        ``controlling_team`` is the engine's own reading of possession, and it
        persists while the ball runs loose, so it says whose ball this is to
        lose. ``closest_to_ball(obs)`` — imported from ``soccer`` — is the other
        way to ask, and it changes the moment somebody out-sprints you. They
        disagree often, and which one you build on is your first real decision.
        """
        actions = TeamAction()
        ours = obs.ball.controlling_team == 0
        chaser = obs.closest_my_player_to(obs.ball.position)

        for player in obs.my_players:
            nearest = player.id == chaser.id

            if ours and nearest:
                # Ours, and I am the one on it: have a go at goal. can_kick is
                # worth checking — a kick from out of range is a wasted tick,
                # and the engine reports it back as a "kick_rejected" event.
                if obs.can_kick(player.id):
                    actions.set(
                        player.id,
                        PlayerAction(
                            movement=direction(player.position, obs.ball.position),
                            kick_direction=direction(player.position, obs.opponent_goal),
                            kick_power=1.0,
                        ),
                    )
                else:
                    actions.move(
                        player.id, direction(player.position, obs.ball.position)
                    )

            elif ours:
                # Ours, and somebody else is on it: get up the pitch. Running at
                # the goal puts all four of you in the same place, which is the
                # first thing to fix — they cannot all receive a pass there.
                actions.move(
                    player.id, direction(player.position, obs.opponent_goal)
                )

            elif nearest:
                # Theirs, and I am the closest: go and win it back.
                actions.move(player.id, direction(player.position, obs.ball.position))

            else:
                # Theirs, and somebody else is closer: get behind the ball.
                actions.move(player.id, direction(player.position, obs.field.my_goal))

        return actions
