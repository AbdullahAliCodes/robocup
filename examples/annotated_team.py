"""A team that explains itself to the viewer.

``simple_team``, decision for decision — the same four branches, the same four
things a player can be doing:

    is the ball ours?
        am I the closest of us to it?   ->  shoot at their goal
        otherwise                       ->  push up towards their goal
    otherwise
        am I the closest of us to it?   ->  go and get it
        otherwise                       ->  drop back towards our goal

The difference is that this one says so. Every tick it labels each player with
the branch they took and draws a line to where they are running, so you can
watch the reasoning rather than infer it from the movement. Comparing the two
files side by side shows you exactly what annotating costs: a dict, a list, and
one call at the end.

Watch it with::

    python -m soccer.cli play examples/annotated_team.py --against balanced \
        --render human --debug-overlays

This is the fastest way to find out why a team is doing something stupid. A
player running to the wrong place looks identical to a player who *decided* on
the wrong place — until you draw the target and see which one it is.

Debug data is non-authoritative. The simulator ignores it completely: it cannot
change physics, scoring or what any controller observes. It is size-limited and
silently dropped if it grows too large, and it is disabled during mass
evaluation, so leaving it in costs you nothing at grading time.
"""

from soccer import PlayerAction, TeamAction, TeamController, direction


class AnnotatedTeam(TeamController):
    name = "annotated_team"
    version = "1"

    def initial_formation(self, field):
        """Where we stand at a kickoff. Optional — delete it for the default."""
        keeper_x = field.my_goal[0] + field.player_radius * 2
        forward_x = -field.centre_circle_radius - 1.0
        return [
            (keeper_x, 0.0),          # keeper
            (-20.0, -12.0),           # left back
            (-20.0, 12.0),            # right back
            (forward_x, -6.0),        # left forward
            (forward_x, 6.0),         # right forward
        ]

    def act(self, obs):
        """Called on every tick. One action, and one label, per player.

        The decisions are `simple_team`'s exactly. Everything to do with
        annotation is the three collections below and the `annotate` call at
        the end — it is deliberately separable, so you can see what is football
        and what is commentary.
        """
        actions = TeamAction()
        roles = {}      # {player_id: what this player is doing}
        targets = {}    # {player_id: where it is running}, drawn as a line
        passes = []     # [{"from": ..., "to": ...}], drawn as an arrow

        ours = obs.ball.controlling_team == 0
        chaser = obs.closest_my_player_to(obs.ball.position)

        for player in obs.my_players:
            nearest = player.id == chaser.id

            if ours and nearest:
                # Ours, and I am the one on it: have a go at goal.
                if obs.can_kick(player.id):
                    roles[player.id] = "shooting"
                    actions.set(
                        player.id,
                        PlayerAction(
                            movement=direction(player.position, obs.ball.position),
                            kick_direction=direction(player.position, obs.opponent_goal),
                            kick_power=1.0,
                        ),
                    )
                    # Draw the shot. Watching where these actually go is how
                    # you find out you are shooting from silly distances.
                    passes.append({"from": player.position, "to": obs.opponent_goal})
                else:
                    roles[player.id] = "carrier"
                    targets[player.id] = obs.ball.position
                    actions.move(
                        player.id, direction(player.position, obs.ball.position)
                    )

            elif ours:
                # Ours, and somebody else is on it: get up the pitch. Watch the
                # target lines here — all four converge on one point, which is
                # the first thing this team gets wrong.
                roles[player.id] = "support"
                targets[player.id] = obs.opponent_goal
                actions.move(
                    player.id, direction(player.position, obs.opponent_goal)
                )

            elif nearest:
                # Theirs, and I am the closest: go and win it back.
                roles[player.id] = "presser"
                targets[player.id] = obs.ball.position
                actions.move(player.id, direction(player.position, obs.ball.position))

            else:
                # Theirs, and somebody else is closer: get behind the ball.
                roles[player.id] = "cover"
                targets[player.id] = obs.field.my_goal
                actions.move(player.id, direction(player.position, obs.field.my_goal))

        # One call, and the viewer draws all of it. `text` is a free-form note
        # for the whole team; there is also a `marks` list, shaped like
        # `passes`, for drawing who you have picked up — this team never marks
        # anybody, so it has nothing honest to put in it.
        return actions.annotate(
            text="ours" if ours else "theirs",
            roles=roles,
            targets=targets,
            passes=passes,
        )
