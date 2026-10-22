"""One entry point for the student distribution.

    python launch.py                     the dashboard: pick two teams, watch
    python launch.py play my-team --against balanced
    python launch.py tournament my-team --against balanced --seeds 1000..1020
    python launch.py validate my-team    does your team play legally?
    python launch.py check               is your submission complete?
    python launch.py new their-team      start a second team to play against
    python launch.py baselines           the reference teams you can play
    python launch.py replay <file.rep>   what is inside a recording
    python launch.py doctor              check this installation

A team is a folder holding `team.py`, and that file is exactly what you hand
in. Name the folder wherever a team is asked for.

On Windows, double-click START.cmd instead. On macOS and Linux, run ./start.sh.
Both land here.
"""

from __future__ import annotations

import os
import runpy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import bootstrap  # noqa: E402  - must follow the sys.path line above

#: Handled by the SDK's own command line, so there is one implementation of
#: them and the distribution cannot drift from the platform.
SDK_COMMANDS = ("play", "validate", "tournament", "replay", "baselines")

DEFAULT_PORT = 8770


def _fail(message: str) -> int:
    sys.stderr.write("\n{}\n\n".format(message))
    return 1


# ---------------------------------------------------------------------------
# Teams
# ---------------------------------------------------------------------------


def _evaluator():
    """The submission contract, as the marker applies it.

    Imported from the copy in this download rather than reimplemented, so a
    submission this says is fine is one the marker also says is fine.
    """
    tools = str(ROOT / "tools")
    if tools not in sys.path:
        sys.path.insert(0, tools)
    from evaluator import metadata, submissions

    return metadata, submissions


def find_teams() -> list:
    """Every submission folder in this download, in name order."""
    return sorted(
        (p for p in ROOT.iterdir() if p.is_dir() and (p / "team.py").is_file()),
        key=lambda p: p.name.lower(),
    )


def resolve_team(value: str) -> str:
    """Turns a folder name into what the SDK loads, leaving anything else alone.

    Students think in teams — the folder they are editing — while the SDK loads
    a file, optionally with a class pinned. Translating here means a baseline
    name, a folder and a path all work wherever a team is asked for.
    """
    if not value or value.startswith("-"):
        return value

    candidate = ROOT / value
    if not candidate.is_dir():
        return value

    team_file = candidate / "team.py"
    if not team_file.is_file():
        return value

    # A class named in a team.toml wins, if the folder happens to have one: a
    # file with two controllers in it is otherwise ambiguous, and the marker
    # resolves it from this same field. Students never write one.
    entry_class = ""
    metadata_file = candidate / "team.toml"
    if metadata_file.is_file():
        try:
            metadata, _ = _evaluator()
            entry_class = metadata.load_metadata(
                metadata_file.read_text(encoding="utf-8")
            ).entry_class
        except Exception:  # noqa: BLE001 - a broken team.toml is `check`'s to report
            entry_class = ""

    spec = str(team_file)
    return "{}:{}".format(spec, entry_class) if entry_class else spec


def _dashboard(argv: list) -> int:
    port = DEFAULT_PORT
    if argv:
        try:
            port = int(argv[0])
        except ValueError:
            return _fail("A port has to be a number, not {!r}.".format(argv[0]))

    server = ROOT / "tools" / "launcher" / "server.py"
    if not server.exists():
        return _fail("The dashboard is missing from this download ({}).".format(server))

    print("\n  Starting the dashboard. Your browser should open by itself;")
    print("  if it does not, go to http://127.0.0.1:{}\n".format(port))
    sys.argv = [str(server), str(port)]
    runpy.run_path(str(server), run_name="__main__")
    return 0


def _check(argv: list) -> int:
    """Reports whether a submission would be accepted, and whether it plays.

    The same two checks the marker runs: the folder's structure, then the team
    actually playing. Finding out here is the entire point of having the
    platform locally.
    """
    try:
        _, submissions = _evaluator()
    except ImportError as exc:
        return _fail("The submission checker is missing from this download: {}".format(exc))

    folders = [ROOT / name for name in argv] if argv else find_teams()
    if not folders:
        return _fail(
            "No teams found. A team is a folder holding team.py.\n"
            "Make one with:  python launch.py new my-team"
        )

    worst = 0
    for folder in folders:
        if not folder.is_dir():
            print("\n  {}: no such folder".format(folder.name))
            worst = 1
            continue

        submission = submissions.inspect(folder)
        print("\n  {}".format(folder.name))

        if submission.metadata is not None:
            meta = submission.metadata
            print("    team        {} v{}".format(meta.name, meta.version))
            print("    members     {}".format(meta.member_summary()))

        for error in submission.errors:
            print("    ERROR       {}".format(error.replace("\n", "\n                ")))
        for warning in submission.warnings:
            print("    warning     {}".format(warning))

        if submission.errors:
            worst = 1
            continue

        # Only worth running the team once the folder itself is sound.
        # Overwritten in place on a terminal, and left out entirely when this
        # is piped to a file, where a carriage return is just litter.
        live = sys.stdout.isatty()
        if live:
            print("    playing a short match...", end="", flush=True)
        report = submissions.check_behaviour(submission, root=ROOT, ticks=400)
        if live:
            print("\r" + " " * 36 + "\r", end="")
        if report.get("ok"):
            print("    PASSED      it loads, plays legally and is inside the deadline")
            print("    hand in     {}{}team.py, on Moodle".format(folder.name, os.sep))
        else:
            worst = 1
            for error in report.get("errors", []):
                print("    ERROR       {}".format(error))

    print()
    return worst


def _new(argv: list) -> int:
    """Scaffolds another submission folder, so there is something to play."""
    if not argv:
        return _fail("Name the team:  python launch.py new their-team")

    try:
        _, submissions = _evaluator()
    except ImportError as exc:
        return _fail("The scaffolder is missing from this download: {}".format(exc))

    folder = ROOT / argv[0]
    if folder.exists() and any(folder.iterdir()):
        return _fail("{} already exists and is not empty.".format(folder.name))

    written = submissions.scaffold(folder)
    print("\n  created {}/".format(folder.name))
    for path in written:
        print("    {}".format(path.relative_to(ROOT)))
    print("\n  Edit team.py, then play it:")
    print("    python launch.py play {} --against my-team\n".format(folder.name))
    return 0


def _doctor(info: dict) -> int:
    import shutil

    print("\n  soccer distribution\n")
    print("  python      {}.{}.{}  ({})".format(*sys.version_info[:3], sys.executable))
    print("  platform    {}".format(info["slot"]))
    print("  engine      {}".format(info["cli"]))
    print("  extension   {}".format(info["extension"]))
    print("  viewer      browser")

    try:
        import soccer

        print("  version     {}".format(soccer.__version__))
        print("  baselines   {}".format(", ".join(soccer.baseline_names())))
        ok = True
    except Exception as exc:  # noqa: BLE001 - doctor reports, never raises
        print("\n  PROBLEM: the engine did not load: {}: {}".format(type(exc).__name__, exc))
        ok = False

    if shutil.which("soccer-cli") is None:
        print("\n  PROBLEM: soccer-cli is not on PATH.")
        ok = False

    teams = [p.name for p in find_teams()]
    print("  your teams  {}".format(", ".join(teams) if teams else "none yet"))

    print("\n  {}\n".format("Everything works." if ok else "Something is wrong — see above."))
    return 0 if ok else 1


def main(argv: list | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    # Relative team paths are how everything is written, and a double-clicked
    # launcher starts wherever the shell happened to be.
    os.chdir(ROOT)

    try:
        info = bootstrap.ensure_engine(ROOT)
    except bootstrap.BootstrapError as exc:
        return _fail(str(exc))

    command = argv[0] if argv else "dashboard"
    rest = argv[1:]

    if command in ("help", "-h", "--help"):
        print(__doc__)
        return 0
    if command == "doctor":
        return _doctor(info)
    if command == "check":
        return _check(rest)
    if command == "new":
        return _new(rest)
    if command in ("dashboard", "ui", "web"):
        return _dashboard(rest)
    if command in SDK_COMMANDS:
        from soccer.cli import main as sdk_main

        # A team is named by its folder here and by its file to the SDK, and
        # `--against` takes one too.
        rest = [resolve_team(value) for value in rest]
        return sdk_main([command] + rest)

    return _fail(
        "Unknown command {!r}.\n"
        "Try one of: dashboard, {}, doctor\n"
        "or run:  python launch.py help".format(command, ", ".join(SDK_COMMANDS))
    )


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        sys.stderr.write("\nstopped\n")
        raise SystemExit(130)
