"""Playing matches and evaluating over seed sets.

The failure policy from the design document is applied here, because this is
the layer that can actually observe a Python controller failing:

===============================  ==========================================
condition                        response
===============================  ==========================================
the method raises                log it, reuse the previous valid action,
                                 increment the error counter
the decision exceeds its         reuse the previous valid action, increment
deadline                         the timeout counter
too many consecutive failures    forfeit the match
an invalid numeric value         the engine rejects it and uses a no-op for
                                 that player
a missing player action          no-op for that player; the rest stand
an oversized debug payload       discard the debug data, keep the actions
===============================  ==========================================

None of these propagate out of :func:`play_match`. A submission that crashes on
every tick produces a forfeit and a result record, not an exception.

Evaluation parallelises across *processes*, not threads. A Python controller
holds the interpreter lock, so a thread pool would serialise; one match per
worker process scales properly and matches how the server schedules work.
"""

from __future__ import annotations

import os
import sys
import time
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from statistics import mean, stdev
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Union

from . import _engine as _native

from .controller import TeamController
from .env import SoccerEnv
from .errors import SubmissionError
from .helpers import load_controller

#: A team is either a live controller or a specification string. Strings are
#: what evaluation uses, because a controller cannot be sent to a subprocess.
TeamSpec = Union[TeamController, str]

#: Reference teams the engine knows and this package does not offer. `elite` is
#: where the next reference opponent is developed: it changes between builds, so
#: practising against it is practising against a moving target, and a result
#: from it means nothing next week.
_INTERNAL_BASELINES = ("elite",)


def baseline_names() -> Tuple[str, ...]:
    """The reference teams on offer.

    Asked of the engine rather than listed here, so a baseline that is added
    cannot go missing — minus the ones that are the platform's own working
    copies rather than opponents to practise against.

    This is the list every student-facing surface publishes and validates
    against: the dashboard's dropdown, ``python -m soccer.cli``, the league
    site, and the names an unknown team is reported against. The platform's own
    tools ask the engine directly and still reach every baseline it builds.
    """
    return tuple(
        name for name in _native.baseline_names()
        if name not in _INTERNAL_BASELINES
    )


def is_offered(spec: str) -> bool:
    """Whether a typed team name is one this package puts on offer.

    Only meaningful for a bare name. A path or a module is a team of somebody's
    own, and whether it loads is a question for :func:`load_controller`.
    """
    if any(mark in spec for mark in ("/", "\\", ".", ":")):
        return True
    return spec in baseline_names()


# ---------------------------------------------------------------------------
# One match
# ---------------------------------------------------------------------------


@dataclass
class MatchOutcome:
    """The result of one match, plus anything collected while it ran."""

    result: Dict[str, Any]
    #: Populated only when ``collect_frames`` was requested in rgb_array mode.
    frames: List[Tuple[int, int, bytes]] = field(default_factory=list)

    @property
    def score(self) -> Tuple[int, int]:
        return tuple(self.result["score"])

    @property
    def winner(self) -> Optional[int]:
        a, b = self.score
        return None if a == b else (0 if a > b else 1)

    @property
    def replay_path(self) -> Optional[str]:
        return self.result.get("replay_path")

    def summary(self) -> str:
        a, b = self.score
        names = self.result.get("team_ids", ["team_a", "team_b"])
        return (
            f"{names[0]} {a} - {b} {names[1]}  "
            f"({self.result['ticks']} ticks, {self.result['terminal_reason']})"
        )


def play_match(
    team_a: TeamSpec,
    team_b: TeamSpec,
    seed: int = 1234,
    kickoff_team: int = 0,
    *,
    render_mode: Optional[str] = None,
    record_replay: Optional[str] = None,
    record_debug: bool = False,
    config_path: Optional[str] = None,
    max_ticks: Optional[int] = None,
    players_per_team: Optional[int] = None,
    per_decision_timeout_ms: Optional[float] = None,
    viewer_address: Optional[str] = None,
    viewer_timeout_secs: int = 30,
    collect_frames: bool = False,
    match_id: Optional[str] = None,
    aerial: Optional[bool] = None,
) -> MatchOutcome:
    """Plays one complete match and returns its record.

    ``team_a`` and ``team_b`` may be live controllers, ``"module:Class"``
    specifications, ``"file.py:Class"`` paths, or the name of a built-in
    baseline such as ``"balanced"``.
    """
    # A side named after a built-in baseline is driven inside the engine; only
    # the other side crosses the language boundary each tick.
    controllers: List[Optional[TeamController]] = [_resolve(team_a), _resolve(team_b)]
    names = [
        team_a if controllers[0] is None else controllers[0].name,
        team_b if controllers[1] is None else controllers[1].name,
    ]

    env = SoccerEnv(
        players_per_team=players_per_team,
        max_ticks=max_ticks,
        render_mode=render_mode,
        record_replay=record_replay,
        record_debug=record_debug,
        config_path=config_path,
        per_decision_timeout_ms=per_decision_timeout_ms,
        viewer_address=viewer_address,
        viewer_timeout_secs=viewer_timeout_secs,
        team_ids=(names[0], names[1]),
        match_id=match_id or f"{names[0]}-vs-{names[1]}-{seed}",
        aerial=aerial,
    )
    for team, spec in ((0, team_a), (1, team_b)):
        if controllers[team] is None:
            env.use_baseline(team, spec)

    frames: List[Tuple[int, int, bytes]] = []
    try:
        # Formations are settled before the kickoff state is built, so they have
        # to be asked for before `reset`. A team that raises here simply takes
        # the default formation: a bad line-up is not worth forfeiting a match
        # over, and the engine reports the failure like any other.
        for team, controller in enumerate(controllers):
            if controller is None:
                continue
            spots = _formation(controller, env)
            if spots is not None:
                env.set_formation(team, spots)

        for controller in controllers:
            if controller is not None:
                controller.reset(seed)
        observations = env.reset(seed=seed, kickoff_team=kickoff_team)

        # The previous valid action per team, reused whenever a decision fails.
        previous: List[Any] = [
            TeamController.idle(observations["team_a"]),
            TeamController.idle(observations["team_b"]),
        ]

        while not env.finished:
            actions: Dict[str, Any] = {}
            for team, key in ((0, "team_a"), (1, "team_b")):
                controller = controllers[team]
                if controller is None:
                    # Driven by the engine; it records its own timing.
                    continue
                action, elapsed_ms, error = _decide(controller, observations[key])
                if error is None:
                    previous[team] = action
                else:
                    # Repeat the previous valid action rather than freezing the
                    # team; a single bad tick should not decide a match.
                    action = previous[team]
                env.record_decision(team, elapsed_ms, error)
                actions[key] = action

            if env.finished:
                # A forfeit can land during decision recording.
                break
            observations, _ = env.step(actions)

            if collect_frames and render_mode == "rgb_array":
                frames.append(env.render())

        result = env.result()
    finally:
        env.close()

    for controller in controllers:
        if controller is None:
            continue
        try:
            controller.on_match_end(result)
        except Exception:  # noqa: BLE001 - a reporting hook must not fail a match
            traceback.print_exc()

    return MatchOutcome(result=result, frames=frames)


def _decide(controller: TeamController, observation) -> Tuple[Any, float, Optional[str]]:
    """Calls a controller, timing it and containing any exception."""
    started = time.perf_counter()
    try:
        action = controller.act(observation)
    except BaseException as exc:  # noqa: BLE001 - untrusted code, contain everything
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        message = f"{type(exc).__name__}: {exc}"
        return None, elapsed_ms, message
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    if action is None:
        return None, elapsed_ms, "controller returned None instead of a TeamAction"
    return action, elapsed_ms, None


def _formation(controller: TeamController, env) -> Optional[List[Tuple[float, float]]]:
    """Asks a controller for its kickoff formation, containing any failure.

    Anything the engine cannot use — an exception, a non-sequence, a point that
    is not a pair of numbers — falls back to the default formation rather than
    failing the match. ``soccer validate`` is where a student is told about it;
    by kickoff it is too late to be useful, and a match is not the place to
    argue about it.
    """
    try:
        spots = controller.initial_formation(env.field)
    except BaseException as exc:  # noqa: BLE001 - untrusted code, contain everything
        print(
            f"{controller.name}: initial_formation raised, using the default "
            f"formation - {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return None
    if spots is None:
        return None
    try:
        return [(float(x), float(y)) for x, y in spots]
    except (TypeError, ValueError) as exc:
        print(
            f"{controller.name}: initial_formation must return (x, y) pairs, "
            f"using the default formation - {exc}",
            file=sys.stderr,
        )
        return None


def _resolve(team: TeamSpec) -> Optional[TeamController]:
    """Turns a team specification into a live controller.

    Returns ``None`` for a built-in baseline, which is driven inside the engine
    rather than from Python.
    """
    if isinstance(team, TeamController):
        return team
    if not isinstance(team, str):
        raise TypeError(
            f"a team must be a TeamController or a specification string, "
            f"got {type(team).__name__}"
        )
    # The engine's list, not the offered one: this is the question of who drives
    # the side, and the platform's own tools play baselines that are not on
    # offer. What a student may *ask for* is settled before it reaches here.
    if team in _native.baseline_names():
        return None
    return _load_or_explain(team)


# ---------------------------------------------------------------------------
# Evaluation over a seed set
# ---------------------------------------------------------------------------


def evaluate(
    team_a: str,
    team_b: str,
    seeds: Iterable[int] = range(1000, 1100),
    *,
    swap_sides: bool = True,
    workers: Optional[int] = None,
    render_mode: None = None,
    record_replays: str = "never",
    replay_dir: Optional[str] = None,
    config_path: Optional[str] = None,
    max_ticks: Optional[int] = None,
    players_per_team: Optional[int] = None,
    per_decision_timeout_ms: Optional[float] = None,
    aerial: Optional[bool] = None,
) -> Dict[str, Any]:
    """Evaluates two teams over a seed set, in parallel.

    Both teams are given as specification strings so that each worker process
    can load them independently. When both name built-in baselines the work is
    handed to the engine's own thread pool, which is faster still.

    ``swap_sides`` plays every seed from both field directions with alternating
    kickoff, which removes side and kickoff advantage from the aggregate.
    """
    if render_mode is not None:
        raise ValueError(
            "evaluation is headless by design; use play_match(render_mode=...) "
            "to watch a single match"
        )
    seeds = list(seeds)
    baselines = _native.baseline_names()

    if team_a in baselines and team_b in baselines:
        # Native on both sides: no interpreter lock in the way, so use the
        # engine's parallel batch runner directly.
        return _native.evaluate_baselines(
            team_a,
            team_b,
            seeds,
            swap_sides=swap_sides,
            workers=workers,
            config_path=config_path,
            max_ticks=max_ticks,
            players_per_team=players_per_team,
            record_replays=record_replays,
            replay_dir=replay_dir,
            aerial=aerial,
        )

    # Resolve both teams once, here, before any fixture is scheduled. A typo in
    # a team name should fail immediately with the list of valid baselines,
    # not produce a batch of identical error rows an hour later.
    for spec in (team_a, team_b):
        _check_resolvable(spec, baselines)

    fixtures = _build_fixtures(seeds, swap_sides)
    workers = workers or min(len(fixtures), os.cpu_count() or 1) or 1
    keep_replays = record_replays != "never"

    results: List[Dict[str, Any]] = []
    started = time.perf_counter()
    if workers <= 1:
        for fixture in fixtures:
            results.append(
                _run_fixture(
                    team_a, team_b, fixture, config_path, max_ticks,
                    players_per_team, per_decision_timeout_ms,
                    replay_dir if keep_replays else None,
                    aerial,
                )
            )
    else:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            futures = [
                pool.submit(
                    _run_fixture,
                    team_a,
                    team_b,
                    fixture,
                    config_path,
                    max_ticks,
                    players_per_team,
                    per_decision_timeout_ms,
                    replay_dir if keep_replays else None,
                    aerial,
                )
                for fixture in fixtures
            ]
            for future in as_completed(futures):
                results.append(future.result())

    # Completion order is not reproducible; the report must be.
    results.sort(key=lambda r: r["match_id"])
    wall_time_ms = (time.perf_counter() - started) * 1000.0

    return {
        "team_a": team_a,
        "team_b": team_b,
        "engine_version": _native.ENGINE_VERSION,
        "config_hash": results[0]["config_hash"] if results else "",
        "summary": _summarise(results),
        "results": results,
        "wall_time_ms": wall_time_ms,
        "workers": workers,
    }


def _check_resolvable(spec: str, baselines: Sequence[str]) -> None:
    """Raises a clear error if `spec` names neither a baseline nor a loadable team."""
    if spec in baselines:
        return
    _load_or_explain(spec)


def _load_or_explain(spec: str) -> TeamController:
    """Loads a team, or fails with the names that would have worked.

    A bare word that names no baseline is a mistyped opponent, and the list of
    names that do work is the answer to it. That list is the offered one, so a
    name the engine knows but this package does not offer is refused like any
    other unknown team. A path or a module is a different mistake — something
    inside the file — and a list of baselines under it would only distract.
    """
    try:
        return load_controller(spec)
    except SubmissionError as exc:
        if any(mark in spec for mark in ("/", "\\", ".", ":")):
            raise
        raise SubmissionError(
            f"{exc}\n\nValid baseline names are: {', '.join(baseline_names())}"
        ) from exc


def _build_fixtures(seeds: Sequence[int], swap_sides: bool) -> List[Dict[str, Any]]:
    fixtures: List[Dict[str, Any]] = []
    for seed in seeds:
        fixtures.append({"seed": seed, "swapped": False, "kickoff_team": 0})
        if swap_sides:
            fixtures.append({"seed": seed, "swapped": True, "kickoff_team": 1})
    return fixtures


def _run_fixture(
    team_a: str,
    team_b: str,
    fixture: Dict[str, Any],
    config_path: Optional[str],
    max_ticks: Optional[int],
    players_per_team: Optional[int],
    per_decision_timeout_ms: Optional[float],
    replay_dir: Optional[str],
    aerial: Optional[bool] = None,
) -> Dict[str, Any]:
    """Plays one fixture. Runs in a worker process, so it must be importable
    at module level and take only picklable arguments."""
    swapped = fixture["swapped"]
    engine_a, engine_b = (team_b, team_a) if swapped else (team_a, team_b)
    match_id = "{}-vs-{}-{:010d}-{}".format(
        team_a, team_b, fixture["seed"], "away" if swapped else "home"
    )

    replay_path = None
    if replay_dir:
        os.makedirs(replay_dir, exist_ok=True)
        replay_path = os.path.join(replay_dir, f"{match_id}.rep")

    try:
        outcome = play_match(
            engine_a,
            engine_b,
            seed=fixture["seed"],
            kickoff_team=fixture["kickoff_team"],
            record_replay=replay_path,
            config_path=config_path,
            aerial=aerial,
            max_ticks=max_ticks,
            players_per_team=players_per_team,
            per_decision_timeout_ms=per_decision_timeout_ms,
            match_id=match_id,
        )
        result = outcome.result
    except Exception as exc:  # noqa: BLE001 - a broken fixture still gets a row
        return {
            "match_id": match_id,
            "engine_version": _native.ENGINE_VERSION,
            "config_hash": "",
            "team_ids": [team_a, team_b],
            "seed": fixture["seed"],
            "sides_swapped": swapped,
            "score": [0, 0],
            "goal_difference": 0,
            "ticks": 0,
            "stats": [{}, {}],
            "platform_error": f"{type(exc).__name__}: {exc}",
        }

    if swapped:
        # Undo the swap so score[0] always belongs to the team the caller named
        # team_a, whichever side it actually played.
        result["score"] = [result["score"][1], result["score"][0]]
        result["stats"] = [result["stats"][1], result["stats"][0]]
        result["team_ids"] = [team_a, team_b]
        result["goal_difference"] = result["score"][0] - result["score"][1]
    result["sides_swapped"] = swapped
    result["match_id"] = match_id
    return result


def _pool_team(results: Sequence[Dict[str, Any]], team: int) -> Dict[str, Any]:
    """Pools one team's metrics across a batch.

    Rates are pooled, not averaged-of-averages: mean pass length is the total
    distance over the total completions. A match with one long pass would
    otherwise count as heavily as a match with thirty.
    """
    if not results:
        return {}

    def total(key: str) -> float:
        return sum(float((r.get("stats") or [{}, {}])[team].get(key, 0) or 0) for r in results)

    matches = len(results)
    ticks = sum(int(r.get("ticks", 0)) for r in results)
    goals = sum(int(r["score"][team]) for r in results)
    attempted = total("passes_attempted")
    completed = total("passes_completed")
    intercepted = total("passes_intercepted")
    # Uncollected is whatever is left: attempts nobody reached in time.
    uncollected = max(attempted - completed - intercepted, 0.0)
    dribbles = total("dribbles")
    tackles = total("tackles_attempted")
    tackles_won = total("tackles_won")

    per_match = lambda value: value / matches
    per_tick = lambda value: (value / ticks) if ticks else 0.0
    ratio = lambda numerator, denominator: (numerator / denominator) if denominator else 0.0

    def latency(key: str) -> List[float]:
        return [
            float((r.get("stats") or [{}, {}])[team].get("decision_time_ms", {}).get(key, 0) or 0)
            for r in results
        ]

    latency_count = sum(latency("count"))
    latency_total = sum(latency("total_ms"))
    latency_max = max(latency("max_ms") or [0.0])

    return {
        "goals_per_match": per_match(goals),
        "possession_fraction": per_tick(total("possession_ticks")),
        "control_fraction": per_tick(total("controlled_ticks")),
        "territory_fraction": per_tick(total("territory_ticks")),
        "shots_per_match": per_match(total("shots")),
        "shots_blocked_per_match": per_match(total("shots_blocked")),
        "passes_attempted_per_match": per_match(attempted),
        "passes_completed_per_match": per_match(completed),
        "passes_intercepted_per_match": per_match(intercepted),
        "passes_uncollected_per_match": per_match(uncollected),
        "pass_completion": ratio(completed, attempted),
        "mean_pass_distance": ratio(total("pass_distance"), completed),
        "dribbles_per_match": per_match(dribbles),
        "mean_dribble_distance": ratio(total("dribble_distance"), dribbles),
        "turnovers_per_match": per_match(total("turnovers")),
        # Kickoff encroachments. Named and placed as the native batch runner
        # has them, so a pooled row means the same thing whichever side of the
        # bindings produced it.
        "fouls_per_match": per_match(total("fouls")),
        "tackles_attempted_per_match": per_match(tackles),
        "tackles_won_per_match": per_match(tackles_won),
        "tackle_success": ratio(tackles_won, tackles),
        "policy_errors": int(total("policy_errors")),
        "policy_timeouts": int(total("policy_timeouts")),
        "decision_mean_ms": ratio(latency_total, latency_count),
        "decision_max_ms": latency_max,
    }


def summarise_results(results: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Pools a set of result rows into one aggregate, as :func:`evaluate` does.

    Public because the mass-evaluation pipeline schedules its own fixtures — one
    pool for a whole tournament rather than one per pairing — and must summarise
    each pairing exactly as a single ``evaluate`` call would, or a tie inside a
    tournament would not be comparable with the same tie run on its own.
    """
    return _summarise(results)


def _summarise(results: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    if not results:
        return {
            "matches": 0,
            "team_a_wins": 0,
            "team_b_wins": 0,
            "draws": 0,
            "goals": [0, 0],
            "mean_goal_difference": 0.0,
            "goal_difference_ci95": 0.0,
            "team_stats": [{}, {}],
        }

    differences = [r["score"][0] - r["score"][1] for r in results]
    wins_a = sum(1 for d in differences if d > 0)
    wins_b = sum(1 for d in differences if d < 0)
    draws = sum(1 for d in differences if d == 0)
    average = mean(differences)
    ci95 = 0.0
    if len(differences) > 1:
        ci95 = 1.96 * stdev(differences) / (len(differences) ** 0.5)

    def total(index: int, key: str) -> int:
        return sum(int((r.get("stats") or [{}, {}])[index].get(key, 0)) for r in results)

    return {
        "matches": len(results),
        "team_a_wins": wins_a,
        "team_b_wins": wins_b,
        "draws": draws,
        "goals": [
            sum(r["score"][0] for r in results),
            sum(r["score"][1] for r in results),
        ],
        "mean_goal_difference": average,
        "goal_difference_ci95": ci95,
        "policy_errors": [total(0, "policy_errors"), total(1, "policy_errors")],
        "policy_timeouts": [total(0, "policy_timeouts"), total(1, "policy_timeouts")],
        "total_ticks": sum(r["ticks"] for r in results),
        # Every per-team metric, averaged across the batch, matching what the
        # native batch runner reports.
        "team_stats": [_pool_team(results, 0), _pool_team(results, 1)],
    }
