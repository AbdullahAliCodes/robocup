"""Command-line front end for students.

    python -m soccer.cli play my_team.py --against balanced
    python -m soccer.cli play my_team.py --against balanced --render human
    python -m soccer.cli validate my_team.py
    python -m soccer.cli tournament my_team.py --seeds 1000..1020
    python -m soccer.cli replay match.rep
    python -m soccer.cli baselines
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from typing import List, Optional

from . import _engine as _native

from .errors import SubmissionError
from .runner import baseline_names, evaluate, is_offered, play_match
from .validate import PUBLIC_SEEDS, validate

#: Where the viewer listens when `--render human` is used.
DEFAULT_VIEWER_ADDRESS = "127.0.0.1:5551"


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m soccer.cli",
        description="Play, validate and evaluate soccer teams.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    play = sub.add_parser("play", help="play one match")
    play.add_argument("team", help="file.py[:Class], module[:Class], or a baseline name")
    play.add_argument("--against", default="balanced", help="opponent (default: balanced)")
    play.add_argument("--seed", type=int, default=1234)
    play.add_argument("--ticks", type=int, default=None, help="override the tick limit")
    play.add_argument("--players", type=int, default=None, help="players per team")
    play.add_argument(
        "--render",
        choices=("none", "human", "rgb_array"),
        default="none",
        help="none runs headless with no viewer process at all",
    )
    play.add_argument("--replay", default=None, help="record a replay to this path")
    play.add_argument("--debug-overlays", action="store_true", help="record viewer annotations")
    play.add_argument("--kickoff", choices=("a", "b"), default="a")
    play.add_argument("--viewer-address", default=DEFAULT_VIEWER_ADDRESS)
    play.add_argument("--config", default=None, help="TOML profile")
    play.add_argument("--json", action="store_true", help="print the raw result record")

    check = sub.add_parser("validate", help="check a submission before uploading")
    check.add_argument("team")
    check.add_argument("--against", default="balanced")
    check.add_argument("--ticks", type=int, default=400)
    check.add_argument("--deadline-ms", type=float, default=20.0)
    check.add_argument(
        "--json",
        action="store_true",
        help="print the report as JSON (what the course server reads)",
    )

    tourney = sub.add_parser("tournament", help="evaluate over a seed set")
    tourney.add_argument("team")
    tourney.add_argument("--against", default="balanced")
    tourney.add_argument("--seeds", default="1000..1020", help="range START..END")
    tourney.add_argument("--workers", type=int, default=None)
    tourney.add_argument("--no-swap", action="store_true", help="do not play both sides")
    tourney.add_argument("--ticks", type=int, default=None)
    tourney.add_argument("--config", default=None)
    tourney.add_argument("--jsonl", default=None, help="write result rows to this file")

    show = sub.add_parser("replay", help="inspect or view a recorded replay")
    show.add_argument("file")
    show.add_argument("--view", action="store_true", help="open it in the Godot viewer")
    show.add_argument("--events", action="store_true")

    sub.add_parser("baselines", help="list the reference teams")

    args = parser.parse_args(argv)

    try:
        # Both sides of every command that names teams, checked in one place
        # and before anything is started, so a name that is not on offer is a
        # message rather than a match.
        for value in (getattr(args, "team", None), getattr(args, "against", None)):
            if value is not None:
                _check_offered(value)
        if args.command == "play":
            return _command_play(args)
        if args.command == "validate":
            return _command_validate(args)
        if args.command == "tournament":
            return _command_tournament(args)
        if args.command == "replay":
            return _command_replay(args)
        if args.command == "baselines":
            return _command_baselines()
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - a CLI should not show a traceback
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


# ---------------------------------------------------------------------------


def _check_offered(value: str) -> None:
    """Rejects a team name this course does not put on offer.

    A bare word is either a reference opponent or a mistake. The engine builds
    a couple of teams that are the platform's own working copies rather than
    opponents to practise against; naming one here is answered with the list
    that does work, exactly like any other name that is not a team.
    """
    if not is_offered(value):
        raise SubmissionError(
            f"'{value}' is not a team. Give a .py file path, or one of these "
            f"reference opponents: {', '.join(baseline_names())}"
        )


def _command_play(args) -> int:
    render_mode = None if args.render == "none" else args.render
    if render_mode == "human":
        print(f"waiting for the viewer on {args.viewer_address} ...")
        print("start it with:  python -m soccer.cli replay <file> --view")
        print("or launch the Godot viewer project in godot-viewer/")

    outcome = play_match(
        args.team,
        args.against,
        seed=args.seed,
        kickoff_team=0 if args.kickoff == "a" else 1,
        render_mode=render_mode,
        record_replay=args.replay,
        record_debug=args.debug_overlays,
        config_path=args.config,
        max_ticks=args.ticks,
        players_per_team=args.players,
        viewer_address=args.viewer_address,
    )

    if args.json:
        print(json.dumps(outcome.result, indent=2))
        return 0

    result = outcome.result
    names = result["team_ids"]
    print()
    print(f"  {names[0]:<22} {result['score'][0]} - {result['score'][1]} {names[1]:>22}")
    print()
    print(f"  {'':<26}{'you':>10}{'opponent':>12}")
    rows = [
        ("possession", _pct(result, 0, "possession_ticks"), _pct(result, 1, "possession_ticks")),
        ("  of which controlled", _pct(result, 0, "controlled_ticks"), _pct(result, 1, "controlled_ticks")),
        ("territory", _pct(result, 0, "territory_ticks"), _pct(result, 1, "territory_ticks")),
        ("shots", _stat(result, 0, "shots"), _stat(result, 1, "shots")),
        ("passes completed", _passes(result, 0), _passes(result, 1)),
        ("turnovers", _stat(result, 0, "turnovers"), _stat(result, 1, "turnovers")),
        ("fouls", _stat(result, 0, "fouls"), _stat(result, 1, "fouls")),
        ("policy errors", _stat(result, 0, "policy_errors"), _stat(result, 1, "policy_errors")),
        ("policy timeouts", _stat(result, 0, "policy_timeouts"), _stat(result, 1, "policy_timeouts")),
        ("decision mean (ms)", _latency(result, 0), _latency(result, 1)),
    ]
    for label, a, b in rows:
        print(f"  {label:<26}{a:>10}{b:>12}")

    print()
    print(
        f"  {result['ticks']} ticks in {result['wall_time_ms']:.0f} ms, "
        f"ended: {_reason(result['terminal_reason'])}"
    )
    print(f"  engine {result['engine_version']}  config {result['config_hash']}  seed {result['seed']}")
    if result.get("replay_path"):
        print(f"  replay written to {result['replay_path']}")
    return 0


def _command_validate(args) -> int:
    report = validate(
        args.team,
        ticks=args.ticks,
        opponent=args.against,
        deadline_ms=args.deadline_ms,
    )
    if args.json:
        # The mass-evaluation pipeline runs this in a subprocess so that a
        # submission which hangs cannot take the marker down with it, and reads
        # the report from stdout.
        print(json.dumps({
            "ok": report.ok,
            "controller": report.controller,
            "errors": report.errors,
            "warnings": report.warnings,
            "stats": report.stats,
        }))
    else:
        print(report.render())
    return 0 if report.ok else 1


def _command_tournament(args) -> int:
    start, _, end = args.seeds.partition("..")
    if not end:
        raise ValueError(f"--seeds expects START..END, got '{args.seeds}'")
    seeds = range(int(start), int(end))

    report = evaluate(
        args.team,
        args.against,
        seeds=seeds,
        swap_sides=not args.no_swap,
        workers=args.workers,
        config_path=args.config,
        max_ticks=args.ticks,
    )
    summary = report["summary"]

    print(f"{report['team_a']} vs {report['team_b']} over {summary['matches']} matches")
    print(f"  engine {report['engine_version']}  config {report['config_hash']}")
    print()
    win_rate = summary["team_a_wins"] / summary["matches"] * 100 if summary["matches"] else 0.0
    print(
        f"  record        {summary['team_a_wins']}W {summary['draws']}D "
        f"{summary['team_b_wins']}L  (win rate {win_rate:.1f}%)"
    )
    print(f"  goals         {summary['goals'][0]} - {summary['goals'][1]}")
    print(
        f"  goal diff     {summary['mean_goal_difference']:+.3f} "
        f"+/- {summary['goal_difference_ci95']:.3f} (95% CI)"
    )
    errors = summary.get("policy_errors", [0, 0])
    timeouts = summary.get("policy_timeouts", [0, 0])
    print(f"  reliability   errors {errors[0]}, timeouts {timeouts[0]}")
    print()
    print(
        f"  {summary['matches'] / max(report['wall_time_ms'] / 1000.0, 1e-9):.1f} matches/s "
        f"on {report['workers']} workers, {report['wall_time_ms'] / 1000.0:.1f} s wall"
    )

    failures = [r for r in report["results"] if r.get("platform_error")]
    if failures:
        print(f"\n  warning: {len(failures)} matches failed at the platform level")
        for row in failures[:3]:
            print(f"    {row['match_id']}: {row['platform_error']}")

    if args.jsonl:
        with open(args.jsonl, "w", encoding="utf-8") as handle:
            for row in report["results"]:
                handle.write(json.dumps(row) + "\n")
        print(f"  results written to {args.jsonl}")
    return 0


def _command_replay(args) -> int:
    if args.view:
        return _launch_viewer(["--replay", os.path.abspath(args.file)])

    data = _native.read_replay(args.file)
    header = data["header"]
    print(args.file)
    print(f"  format version   {header['replay_format_version']}")
    print(f"  engine           {header['engine_version']}")
    print(f"  config hash      {header['config_hash']}")
    print(f"  seed             {header['seed']}")
    print(f"  teams            {header['team_ids'][0]} vs {header['team_ids'][1]}")
    print(f"  players per side {header['players_per_team']}")
    print(f"  frames           {data['total_frames']} (every {header['frame_interval']} ticks)")
    print(f"  events           {len(data['events'])}")
    print(f"  debug frames     {len(data['debug'])}")

    goals = [e for e in data["events"] if e.get("kind") == "goal"]
    print(f"  goals            {len(goals)}")
    if args.events:
        print("\nevents:")
        for event in data["events"]:
            print(f"  tick {event['tick']:>6}  {event['kind']}")
    return 0


def _launch_viewer(extra_args: List[str]) -> int:
    """Starts the Godot viewer, failing with a clear message if it is missing.

    Human mode must fail comprehensibly when the viewer is unavailable while
    leaving headless mode entirely functional, so this never raises.
    """
    root = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    project = os.path.join(root, "godot-viewer")

    # The student distribution ships the browser viewer instead of Godot, so
    # there is no project here to open. Point at the viewer that is present
    # rather than at an install that would not help.
    if not os.path.exists(os.path.join(project, "project.godot")):
        print(
            "This installation does not include the Godot viewer.\n"
            "Record the match and watch it in the browser viewer instead:\n"
            "    record a replay, then open the dashboard and pick it.\n"
            "Headless play and replay recording are unaffected.",
            file=sys.stderr,
        )
        return 1

    executable = os.environ.get("SOCCER_VIEWER")
    if executable is None:
        executable = shutil.which("soccer-viewer") or shutil.which("godot")
    if executable is None:
        print(
            "The viewer is not available.\n"
            "Set SOCCER_VIEWER to the exported viewer executable, or install Godot 4\n"
            "and run the project in godot-viewer/ directly. Headless play and replay\n"
            "recording work without it.",
            file=sys.stderr,
        )
        return 1

    command = [executable]
    if os.path.basename(executable).lower().startswith("godot"):
        command += ["--path", project]
    command += ["--"] + extra_args
    try:
        return subprocess.call(command)
    except OSError as exc:
        print(f"could not start the viewer: {exc}", file=sys.stderr)
        return 1


def _command_baselines() -> int:
    descriptions = {
        "do_nothing": "no movement or kicks; checks scoring and timeouts",
        "random_legal": "bounded random movement and valid kicks; robustness only",
        "ball_chaser": "everybody runs at the ball; deliberately weak",
        "structured_attack": "one carrier, support runners and covering players",
        "man_marking": "nearest-opponent marking with a goal protector",
        "balanced": "attack/defend shapes with passing; the reference opponent",
        "possession": "keeps the ball: leads its passes and holds rather than hoofing",
        "tactical": "role heatmap off the ball, opponent-aware shots and passes; the strongest",
    }
    print("available baselines:")
    for name in baseline_names():
        print(f"  {name:<20} {descriptions.get(name, '')}")
    print(f"\npublic practice seeds: {', '.join(str(s) for s in PUBLIC_SEEDS)}")
    return 0


# ---------------------------------------------------------------------------


def _stat(result, team: int, key: str) -> str:
    return str(result["stats"][team].get(key, 0))


def _pct(result, team: int, key: str) -> str:
    ticks = max(result["ticks"], 1)
    return f"{result['stats'][team].get(key, 0) / ticks * 100:.1f}%"


def _passes(result, team: int) -> str:
    stats = result["stats"][team]
    return f"{stats.get('passes_completed', 0)}/{stats.get('passes_attempted', 0)}"


def _latency(result, team: int) -> str:
    latency = result["stats"][team].get("decision_time_ms", {})
    count = latency.get("count", 0)
    if not count:
        return "0.000"
    return f"{latency.get('total_ms', 0.0) / count:.3f}"


def _reason(reason) -> str:
    if isinstance(reason, dict):
        key = next(iter(reason))
        if key == "forfeit":
            return f"forfeit by team {reason[key]['team']}"
        return key
    return str(reason)


if __name__ == "__main__":
    raise SystemExit(main())
