"""The environment wrapper students drive directly.

This is a thin convenience layer over the compiled engine. It exists so the
call sequence in the design document works verbatim::

    env = SoccerEnv(players_per_team=5, simulation_hz=20, render_mode=None)
    observation = env.reset(seed=1234)
    while not env.finished:
        action_a = team_a.act(observation["team_a"])
        action_b = team_b.act(observation["team_b"])
        observation, result = env.step({"team_a": action_a, "team_b": action_b})
    env.close()

``render_mode=None`` starts no viewer, opens no window and creates no GPU or
rendering context. Only ``render_mode="human"`` involves Godot at all.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Sequence, Tuple

from . import _engine as _native

from .actions import TeamAction

RenderMode = Optional[str]

_VALID_RENDER_MODES = (None, "human", "replay", "rgb_array")


class SoccerEnv:
    """One match, stepped from Python.

    Parameters mirror the configuration profile; anything left as ``None``
    keeps the value from the loaded profile, which itself defaults to the
    frozen course configuration — under which the ball stays on the ground, so
    every result recorded against it keeps its configuration hash and stays
    reproducible.
    """

    def __init__(
        self,
        players_per_team: Optional[int] = None,
        simulation_hz: Optional[int] = None,
        max_ticks: Optional[int] = None,
        render_mode: RenderMode = None,
        record_replay: Optional[str] = None,
        config_path: Optional[str] = None,
        config_toml: Optional[str] = None,
        rgb_width: int = 640,
        rgb_height: int = 400,
        record_debug: bool = False,
        per_decision_timeout_ms: Optional[float] = None,
        viewer_address: Optional[str] = None,
        viewer_timeout_secs: int = 30,
        team_ids: Optional[Tuple[str, str]] = None,
        match_id: Optional[str] = None,
        aerial: Optional[bool] = None,
    ) -> None:
        if render_mode not in _VALID_RENDER_MODES:
            raise ValueError(
                f"render_mode must be one of {_VALID_RENDER_MODES}, got {render_mode!r}"
            )
        # Recording a replay implies the replay render mode, so that
        # record_replay="game.rep" on its own does what it looks like it does.
        if record_replay is not None and render_mode is None:
            render_mode = "replay"

        self._env = _native.SoccerEnv(
            players_per_team=players_per_team,
            simulation_hz=simulation_hz,
            max_ticks=max_ticks,
            render_mode=render_mode,
            record_replay=record_replay,
            config_path=config_path,
            config_toml=config_toml,
            rgb_width=rgb_width,
            rgb_height=rgb_height,
            record_debug=record_debug,
            per_decision_timeout_ms=per_decision_timeout_ms,
            viewer_address=viewer_address,
            viewer_timeout_secs=viewer_timeout_secs,
            team_ids=team_ids,
            match_id=match_id,
            # Staff-only, and deliberately undocumented: aerial play is not
            # offered to students, nothing student-facing sets this, and the
            # typed actions carry no angle to use it with. It stays for the
            # platform's own tests and the evaluator's --aerial. See "Ground
            # and aerial" in README.md.
            aerial=aerial,
        )
        self._closed = False

    # -- lifecycle --------------------------------------------------------

    def reset(
        self, seed: Optional[int] = None, kickoff_team: Optional[int] = None
    ) -> Dict[str, Any]:
        """Restores the kickoff state and returns the first observations."""
        return self._env.reset(seed=seed, kickoff_team=kickoff_team)

    def step(self, actions: Dict[str, Any]) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
        """Advances one tick.

        ``actions`` maps ``"team_a"`` / ``"team_b"`` to a
        :class:`~soccer.actions.TeamAction`, or to a plain
        ``{player_id: action}`` dictionary.

        Returns ``(observations, result)``. ``result`` is ``None`` until the
        match ends, then the full record.
        """
        payload = {
            key: (value if not isinstance(value, TeamAction) else value)
            for key, value in actions.items()
        }
        observations, done = self._env.step(payload)
        return observations, (self.result() if done else None)

    def use_baseline(self, team: int, name: str) -> None:
        """Hands one side to a built-in reference team.

        That side is then driven inside the engine, so no action need be
        supplied for it in :meth:`step`. This is how a submission plays a
        baseline without a second Python process and without the baseline being
        reimplemented in Python.
        """
        self._env.use_baseline(team, name)

    @property
    def native_teams(self) -> Tuple[Optional[str], Optional[str]]:
        """Which sides, if any, are driven by a built-in baseline."""
        return self._env.native_teams

    @property
    def field(self) -> Any:
        """The pitch, in the canonical frame, readable before :meth:`reset`.

        The same view ``observation.field`` gives, available at the point a
        controller is asked for its formation and there is no observation yet.
        """
        return self._env.field

    def set_formation(self, team: int, spots: Sequence[Tuple[float, float]]) -> int:
        """Lines a side up on a formation of its own choosing.

        ``spots`` is one ``(x, y)`` per player in slot order, in that side's
        canonical frame. Returns how many had to be moved to satisfy the rules
        — inside the pitch, own half, outside the centre circle — since an
        illegal spot is moved to the nearest legal one rather than refused.

        Must be called before :meth:`reset`.
        """
        return self._env.set_formation(team, list(spots))

    def record_decision(
        self, team: int, elapsed_ms: float, error: Optional[str] = None
    ) -> None:
        """Reports how long a controller took, and whether it failed.

        The runner calls this for you. Call it yourself only if you are driving
        the environment by hand and want deadline enforcement and the forfeit
        rule to apply, which is what the server does.
        """
        self._env.record_decision(team, elapsed_ms, error)

    def result(self) -> Dict[str, Any]:
        """The match record, carrying the full reproduction tuple."""
        return self._env.result()

    def close(self) -> None:
        """Finalises any replay and releases resources. Safe to call twice."""
        if not self._closed:
            self._env.close()
            self._closed = True

    def __enter__(self) -> "SoccerEnv":
        return self

    def __exit__(self, *_exc: Any) -> bool:
        self.close()
        return False

    # -- state ------------------------------------------------------------

    @property
    def finished(self) -> bool:
        return self._env.finished

    @property
    def tick(self) -> int:
        return self._env.tick

    @property
    def score(self) -> Tuple[int, int]:
        return self._env.score

    @property
    def seed(self) -> int:
        return self._env.seed

    @property
    def render_mode(self) -> RenderMode:
        return self._env.render_mode

    @property
    def players_per_team(self) -> int:
        return self._env.players_per_team

    @property
    def max_ticks(self) -> int:
        return self._env.max_ticks

    @property
    def config_hash(self) -> str:
        """Identifies the exact rules in force. Recorded with every result."""
        return self._env.config_hash

    @property
    def engine_version(self) -> str:
        return self._env.engine_version

    def config(self) -> Dict[str, Any]:
        """The effective configuration as a dictionary."""
        return self._env.config()

    def state_hash(self) -> int:
        """Digest of the authoritative state, for regression tests."""
        return self._env.state_hash()

    def render(self) -> Tuple[int, int, bytes]:
        """The latest frame as ``(width, height, rgb_bytes)``.

        Only available with ``render_mode="rgb_array"``. No window or GPU
        context is involved: frames come from a software rasteriser.
        """
        return self._env.render()

    def __repr__(self) -> str:
        return repr(self._env)
