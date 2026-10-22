"""Local launcher for the soccer platform.

Serves a dashboard on 127.0.0.1 where you pick two teams and press a button. The
page cannot start processes by itself, so this server does it: the browser posts
a request here, and this process spawns `soccer-cli` and, when asked, the Godot
viewer.

Run it:

    python tools/launcher/server.py

then open http://127.0.0.1:8770 .

Security posture: this binds the loopback interface only and never invokes a
shell. Team names are resolved against the engine's own baseline list or checked
to be a real ``.py`` file inside the repository, so the page cannot be talked
into running an arbitrary program. It is a developer convenience, not something
to expose on a network.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
PAGE = HERE / "index.html"

DEFAULT_PORT = 8770
DEFAULT_VIEWER_ADDRESS = "127.0.0.1:5551"
#: A match is bounded well above the longest sensible run, so a wedged process
#: cannot occupy a worker for ever.
MATCH_TIMEOUT_SECONDS = 600
#: A seed set is hundreds of matches, and a Python controller is a few hundred
#: times slower than a native one, so it gets its own far looser bound.
BATCH_TIMEOUT_SECONDS = 3600

def _load_baselines() -> List[str]:
    """The reference teams, asked of the SDK rather than hardcoded here.

    A duplicated list silently goes stale the moment a baseline is added, and
    then the launcher rejects a team the engine is perfectly happy to play. The
    SDK's list rather than the engine's, so the dashboard offers exactly what
    `python -m soccer.cli` does and neither shows the platform's working copies.
    """
    try:
        sys.path.insert(0, str(ROOT / "student-sdk"))
        import soccer

        return list(soccer.baseline_names())
    except Exception:  # noqa: BLE001 - the launcher must still start
        return [
            "balanced",
            "structured_attack",
            "man_marking",
            "ball_chaser",
            "random_legal",
            "do_nothing",
        ]


BASELINES = _load_baselines()


class LauncherError(Exception):
    """Something the user can fix, reported to the page as a plain message."""


# ---------------------------------------------------------------------------
# Relaying a live match to the browser
# ---------------------------------------------------------------------------


class LiveSession:
    """Attaches to a running simulator and buffers what it sends.

    The browser cannot open a TCP socket, so it cannot be a viewer itself. This
    stands in as one: it speaks the viewer protocol from
    `sim-core/src/viewer.rs` — a 4-byte little-endian length followed by one
    UTF-8 JSON object — and republishes the snapshots over HTTP for the page to
    poll.

    Being a viewer, it is strictly read-only with respect to the match. If it
    falls behind or dies the simulator carries on, exactly as a crashed Godot
    window would.
    """

    #: Frames are ~1 KB each; a full match is a few thousand. Bounded so a
    #: forgotten session cannot grow without limit.
    MAX_FRAMES = 20_000

    def __init__(self, address: str, speed: int) -> None:
        self.address = address
        self.speed = speed
        self.header: Optional[Dict[str, Any]] = None
        self.frames: List[Dict[str, Any]] = []
        self.finished = False
        self.error: Optional[str] = None
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        host, _, port = self.address.partition(":")
        deadline = time.time() + 30
        sock = None
        while time.time() < deadline and sock is None:
            try:
                sock = socket.create_connection((host, int(port)), timeout=2)
            except OSError:
                time.sleep(0.05)
        if sock is None:
            with self._lock:
                self.error = f"could not attach to the simulator on {self.address}"
                self.finished = True
            return

        try:
            # Ask the simulator to pace itself; it owns pacing in live mode.
            self._send(sock, {"type": "set_speed", "speed": float(self.speed)})
            buffer = b""
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                buffer += chunk
                while len(buffer) >= 4:
                    (length,) = struct.unpack_from("<I", buffer, 0)
                    if len(buffer) < 4 + length:
                        break
                    message = json.loads(buffer[4 : 4 + length].decode("utf-8"))
                    buffer = buffer[4 + length :]
                    self._accept(message)
        except (OSError, ValueError) as exc:
            with self._lock:
                self.error = str(exc)
        finally:
            with self._lock:
                self.finished = True
            try:
                sock.close()
            except OSError:
                pass

    @staticmethod
    def _send(sock: socket.socket, message: Dict[str, Any]) -> None:
        payload = json.dumps(message).encode("utf-8")
        sock.sendall(struct.pack("<I", len(payload)) + payload)

    def _accept(self, message: Dict[str, Any]) -> None:
        kind = message.get("type")
        with self._lock:
            if kind == "hello":
                self.header = message
            elif kind == "snapshot":
                if len(self.frames) < self.MAX_FRAMES:
                    self.frames.append(message)
            elif kind == "end":
                self.summary = message
                self.finished = True

    def state(self, since: int) -> Dict[str, Any]:
        with self._lock:
            return {
                "header": self.header,
                "frames": self.frames[since:],
                "total": len(self.frames),
                "finished": self.finished,
                "error": self.error,
            }


#: The one live relay at a time. Starting another replaces it.
LIVE: Optional[LiveSession] = None


# ---------------------------------------------------------------------------
# Locating the pieces
# ---------------------------------------------------------------------------


def find_cli() -> Path:
    """The soccer-cli binary, preferring a release build."""
    names = ["soccer-cli.exe", "soccer-cli"]
    for profile in ("release", "debug"):
        for name in names:
            candidate = ROOT / "target" / profile / name
            if candidate.exists():
                return candidate
    found = shutil.which("soccer-cli")
    if found:
        return Path(found)
    raise LauncherError(
        "soccer-cli was not found. Build it with:  cargo build --release"
    )


#: Directories never worth searching for a submission: build output, caches and
#: the platform's own source.
SKIP_DIRECTORIES = {
    ".git", ".venv", "venv", "__pycache__", ".pytest_cache", "target", "node_modules",
    "sim-core", "python-bindings", "godot-viewer", "student-sdk", "docs", "configs",
    "replays", "results", "engine", "dist", ".github",
}


def find_submissions() -> List[Dict[str, str]]:
    """Every submission folder that can be played, nearest first.

    A submission is a directory holding `team.py`, which is the same shape the
    marker reads. Without this the dashboard could only ever offer the shipped
    examples, so the one team a student actually wants to watch — their own —
    was the one team not in the list.

    Two levels deep: `my-team/team.py` for a student working in the
    distribution, and `submissions/2412345-alice/team.py` for a marker looking
    at a class.
    """
    found: List[Dict[str, str]] = []
    for directory in sorted(ROOT.iterdir(), key=lambda p: p.name.lower()):
        if not directory.is_dir() or directory.name.lower() in SKIP_DIRECTORIES:
            continue
        candidates = [directory]
        if not (directory / "team.py").is_file():
            candidates = sorted(
                (p for p in directory.iterdir() if p.is_dir()),
                key=lambda p: p.name.lower(),
            )
        for candidate in candidates:
            team = candidate / "team.py"
            if not team.is_file():
                continue
            found.append({
                "label": candidate.name,
                "path": str(team.relative_to(ROOT)).replace("\\", "/"),
            })
    return found


def find_godot() -> Optional[str]:
    """The Godot executable, or None if it cannot be located.

    Checked in order: the SOCCER_VIEWER override, PATH, then the usual install
    locations. Returning None is not an error — headless and replay-recording
    runs do not need it.

    A tree without `godot-viewer/` reports None whatever is installed: the
    student distribution ships the browser viewer only, and an executable with
    no project to open would fail later and less clearly than saying so here.
    """
    if not (ROOT / "godot-viewer" / "project.godot").exists():
        return None
    override = os.environ.get("SOCCER_VIEWER")
    if override and Path(override).exists():
        return override
    for name in ("godot", "godot4", "Godot"):
        found = shutil.which(name)
        if found:
            return found
    guesses = [
        r"C:\Program Files (x86)\Steam\steamapps\common\Godot Engine\godot.windows.opt.tools.64.exe",
        r"C:\Program Files\Godot\godot.exe",
        "/usr/local/bin/godot",
        "/usr/bin/godot",
        str(Path.home() / "Applications" / "Godot.app" / "Contents" / "MacOS" / "Godot"),
    ]
    for guess in guesses:
        if Path(guess).exists():
            return guess
    return None


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def resolve_team(value: str) -> str:
    """Validates a team specification.

    Either a built-in baseline name, or a `.py` file (optionally `:ClassName`)
    that really exists inside the repository. Anything else is rejected rather
    than handed to a subprocess.
    """
    value = (value or "").strip()
    if not value:
        raise LauncherError("a team must be chosen")
    if value in BASELINES:
        return value

    # Split on the *last* colon, and only when what follows is an identifier,
    # so a Windows absolute path survives: "C:\\work\\team.py" must not be read
    # as module "C" with a class name of "\\work\\team.py".
    head, separator, tail = value.rpartition(":")
    path_part = head if separator and tail.isidentifier() else value

    if not path_part.endswith(".py"):
        raise LauncherError(
            f"'{value}' is not a baseline or a .py file. "
            f"Baselines: {', '.join(BASELINES)}"
        )

    candidate = (ROOT / path_part).resolve() if not Path(path_part).is_absolute() else Path(path_part).resolve()
    try:
        candidate.relative_to(ROOT)
    except ValueError:
        raise LauncherError(
            f"'{path_part}' is outside the project directory; refusing to run it"
        ) from None
    if not candidate.exists():
        raise LauncherError(f"no such file: {path_part}")
    return value


def resolve_int(value: Any, name: str, low: int, high: int, default: int) -> int:
    if value in (None, ""):
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise LauncherError(f"{name} must be a whole number") from None
    if not low <= number <= high:
        raise LauncherError(f"{name} must be between {low} and {high}")
    return number


def resolve_address(value: str) -> str:
    value = (value or DEFAULT_VIEWER_ADDRESS).strip()
    if not re.fullmatch(r"(\d{1,3}\.){3}\d{1,3}:\d{1,5}", value):
        raise LauncherError(f"'{value}' is not a HOST:PORT address")
    return value


def resolve_view(request: Dict[str, Any]) -> str:
    """Which Godot view to open: the flat overhead pitch or the isometric one.

    Whitelisted rather than passed through, because this ends up on a command
    line. Anything unrecognised falls back to the flat view rather than being
    an error: a viewer opening in the wrong style is a far better outcome than
    a match that refuses to start.
    """
    return "iso" if str(request.get("godot_view", "")).lower() == "iso" else "flat"


# ---------------------------------------------------------------------------
# Running matches
# ---------------------------------------------------------------------------


def python_team(value: str) -> bool:
    return value not in BASELINES


def run_headless(request: Dict[str, Any]) -> Dict[str, Any]:
    """Plays one match and returns the parsed result record."""
    team_a = resolve_team(request.get("team_a", ""))
    team_b = resolve_team(request.get("team_b", ""))
    seed = resolve_int(request.get("seed"), "seed", 0, 2**31, 1234)
    ticks = resolve_int(request.get("ticks"), "ticks", 20, 200_000, 2400)
    replay = request.get("replay_path") or None

    if python_team(team_a) or python_team(team_b):
        # A submission written in Python has to go through the SDK.
        return run_via_sdk(team_a, team_b, seed, ticks, replay, request)

    command = [
        str(find_cli()), "play",
        "--team-a", team_a,
        "--team-b", team_b,
        "--seed", str(seed),
        "--ticks", str(ticks),
    ]
    if replay:
        command += ["--replay", replay, "--debug-overlays"]

    completed = subprocess.run(
        command, cwd=ROOT, capture_output=True, text=True,
        timeout=MATCH_TIMEOUT_SECONDS,
    )
    if completed.returncode != 0:
        raise LauncherError(completed.stderr.strip() or "the match failed")
    return {"kind": "text", "output": completed.stdout, "replay_path": replay}


def run_via_sdk(
    team_a: str, team_b: str, seed: int, ticks: int,
    replay: Optional[str], request: Dict[str, Any],
) -> Dict[str, Any]:
    """Plays a match involving a Python submission, in a worker process.

    Run out-of-process deliberately: a student controller is untrusted enough
    that it should not be imported into the launcher.
    """
    script = (
        "import json,sys\n"
        "sys.path.insert(0, r'{sdk}')\n"
        "import soccer\n"
        "out = soccer.play_match({a!r}, {b!r}, seed={seed}, max_ticks={ticks},"
        " record_replay={replay!r}, record_debug=bool({replay!r}))\n"
        "print('@@RESULT@@' + json.dumps(out.result))\n"
    ).format(
        sdk=str(ROOT / "student-sdk"),
        a=team_a, b=team_b, seed=seed, ticks=ticks, replay=replay,
    )
    completed = subprocess.run(
        [sys.executable, "-c", script], cwd=ROOT, capture_output=True, text=True,
        timeout=MATCH_TIMEOUT_SECONDS,
    )
    if completed.returncode != 0:
        raise LauncherError(completed.stderr.strip()[-2000:] or "the match failed")
    for line in completed.stdout.splitlines():
        if line.startswith("@@RESULT@@"):
            return {
                "kind": "result",
                "result": json.loads(line[len("@@RESULT@@"):]),
                "replay_path": replay,
            }
    raise LauncherError("the match produced no result record")


def run_live(request: Dict[str, Any]) -> Dict[str, Any]:
    """Starts a simulator that waits for the viewer, then starts the viewer."""
    team_a = resolve_team(request.get("team_a", ""))
    team_b = resolve_team(request.get("team_b", ""))
    seed = resolve_int(request.get("seed"), "seed", 0, 2**31, 1234)
    ticks = resolve_int(request.get("ticks"), "ticks", 20, 200_000, 2400)
    speed = resolve_int(request.get("speed"), "speed", 1, 16, 4)
    address = resolve_address(request.get("viewer_address", ""))

    use_browser = request.get("viewer") == "browser"
    godot = None if use_browser else find_godot()
    if not use_browser and godot is None:
        raise LauncherError(
            "Godot was not found. Set the SOCCER_VIEWER environment variable to "
            "the executable, or put it on PATH, or choose the browser viewer. "
            "Headless and replay modes work without any viewer."
        )
    if python_team(team_a) or python_team(team_b):
        raise LauncherError(
            "live view currently supports baseline-versus-baseline matches. "
            "Record a replay of your team instead, then watch that."
        )

    live_command = [
        str(find_cli()), "play",
        "--team-a", team_a, "--team-b", team_b,
        "--seed", str(seed), "--ticks", str(ticks),
        "--viewer", address, "--debug-overlays",
    ]
    simulator = subprocess.Popen(
        live_command,
        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    # Give the simulator a moment to bind before the viewer dials in.
    time.sleep(1.0)

    if use_browser:
        global LIVE
        LIVE = LiveSession(address, speed)
        return {
            "kind": "launched",
            "open": "/viewer.html?live=1",
            "message": (
                f"Simulator started ({team_a} vs {team_b}, seed {seed}); "
                f"streaming to the browser viewer."
            ),
            "simulator_pid": simulator.pid,
        }

    viewer = subprocess.Popen(
        [godot, "--path", str(ROOT / "godot-viewer"), "--",
         "--connect", address, "--speed", str(speed),
         "--view", resolve_view(request)],
        cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return {
        "kind": "launched",
        "message": (
            f"Simulator and viewer started ({team_a} vs {team_b}, seed {seed}). "
            f"The viewer window should appear; press space to pause, "
            f"right-arrow to step."
        ),
        "simulator_pid": simulator.pid,
        "viewer_pid": viewer.pid,
    }


def run_replay(request: Dict[str, Any]) -> Dict[str, Any]:
    """Records a match, then opens it in the viewer."""
    replay_dir = ROOT / "replays"
    replay_dir.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    team_a = resolve_team(request.get("team_a", ""))
    team_b = resolve_team(request.get("team_b", ""))
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", f"{team_a}-vs-{team_b}")
    path = replay_dir / f"{safe}-{stamp}.rep"

    request = dict(request, replay_path=str(path))
    played = run_headless(request)

    speed = resolve_int(request.get("speed"), "speed", 1, 16, 4)

    if request.get("viewer") == "browser":
        played["open"] = f"/viewer.html?replay={path.name}&speed={speed}"
        played["message"] = f"Recorded {path.name}; opening it in the browser viewer."
        return played

    godot = find_godot()
    if godot is None:
        played["message"] = (
            f"Recorded {path.name}, but Godot was not found so it cannot be "
            f"opened. Set SOCCER_VIEWER, put godot on PATH, or choose the "
            f"browser viewer."
        )
        return played

    subprocess.Popen(
        [godot, "--path", str(ROOT / "godot-viewer"), "--",
         "--replay", str(path), "--speed", str(speed),
         "--view", resolve_view(request)],
        cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    played["message"] = f"Recorded {path.name} and opened it in the viewer."
    return played


def run_batch(request: Dict[str, Any]) -> Dict[str, Any]:
    """Evaluates the pairing over a seed set, whatever the teams are written in.

    Handed to ``soccer.evaluate``, which already knows the difference: two
    baselines go to the engine's own parallel batch runner, and anything
    involving a Python controller goes to a process pool. One path here rather
    than two means a student's team is evaluated on exactly the terms a
    baseline is, which is the comparison the numbers are for.

    Run out-of-process for the same reason a single match is: a student
    controller is untrusted enough that it should not be imported here.
    """
    team_a = resolve_team(request.get("team_a", ""))
    team_b = resolve_team(request.get("team_b", ""))
    seed = resolve_int(request.get("seed"), "seed", 0, 2**31, 1000)
    count = resolve_int(request.get("matches"), "matches", 2, 500, 20)
    ticks = resolve_int(request.get("ticks"), "ticks", 20, 200_000, 2400)

    script = (
        "import json,sys\n"
        "sys.path.insert(0, r'{sdk}')\n"
        "import soccer\n"
        "report = soccer.evaluate({a!r}, {b!r}, seeds=range({start}, {end}),"
        " max_ticks={ticks})\n"
        # The per-match rows are the bulk of the payload and the page shows
        # aggregates, so only the failures travel back.
        "report['results'] = [r for r in report['results'] if r.get('platform_error')][:5]\n"
        "print('@@REPORT@@' + json.dumps(report))\n"
    ).format(
        sdk=str(ROOT / "student-sdk"),
        a=team_a, b=team_b, start=seed, end=seed + count, ticks=ticks,
    )
    completed = subprocess.run(
        [sys.executable, "-c", script], cwd=ROOT, capture_output=True, text=True,
        timeout=BATCH_TIMEOUT_SECONDS,
    )
    if completed.returncode != 0:
        raise LauncherError(completed.stderr.strip()[-2000:] or "the batch failed")
    for line in completed.stdout.splitlines():
        if line.startswith("@@REPORT@@"):
            return {"kind": "batch", "report": json.loads(line[len("@@REPORT@@"):])}
    raise LauncherError("the batch produced no report")


ACTIONS = {
    "headless": run_headless,
    "live": run_live,
    "replay": run_replay,
    "batch": run_batch,
}


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    server_version = "SoccerLauncher/1.0"

    def log_message(self, format: str, *args: Any) -> None:
        # One tidy line per request instead of the default noise.
        sys.stderr.write(f"  {self.command} {self.path} -> {args[1] if len(args) > 1 else ''}\n")

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload: Dict[str, Any]) -> None:
        self._send(status, json.dumps(payload).encode("utf-8"), "application/json")

    def _serve_replay(self, query: Dict[str, List[str]]) -> None:
        """Decodes a replay with the engine's own reader and returns JSON.

        Deliberately not re-implemented in JavaScript: a third parser for the
        binary format would be a third thing to keep in step, and the engine
        already has one that is tested against the writer.
        """
        name = (query.get("name") or [""])[0]
        if not name or "/" in name or "\\" in name or name.startswith("."):
            self._json(400, {"error": "a replay name is required"})
            return
        path = (ROOT / "replays" / name).resolve()
        try:
            path.relative_to((ROOT / "replays").resolve())
        except ValueError:
            self._json(400, {"error": "replay is outside the replays directory"})
            return
        if not path.exists():
            self._json(404, {"error": f"no such replay: {name}"})
            return
        try:
            from soccer import _engine

            data = _engine.read_replay(str(path), True)
        except Exception as exc:  # noqa: BLE001
            self._json(500, {"error": f"cannot read replay: {exc}"})
            return
        self._json(200, data)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)

        if parsed.path == "/viewer.html":
            page = HERE / "viewer.html"
            if not page.exists():
                self._send(500, b"viewer.html is missing", "text/plain")
                return
            self._send(200, page.read_bytes(), "text/html; charset=utf-8")
            return
        if parsed.path == "/api/replay":
            self._serve_replay(query)
            return
        if parsed.path == "/api/replays":
            directory = ROOT / "replays"
            names = sorted(
                (p.name for p in directory.glob("*.rep")), reverse=True
            ) if directory.exists() else []
            self._json(200, {"replays": names})
            return
        if parsed.path == "/api/live":
            if LIVE is None:
                self._json(200, {"error": "no live match is running"})
                return
            since = int((query.get("since") or ["0"])[0])
            self._json(200, LIVE.state(since))
            return

        if self.path in ("/", "/index.html"):
            if not PAGE.exists():
                self._send(500, b"index.html is missing", "text/plain")
                return
            self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
            return
        if self.path == "/api/environment":
            godot = find_godot()
            try:
                cli: Optional[str] = str(find_cli())
                cli_error = None
            except LauncherError as exc:
                cli, cli_error = None, str(exc)
            self._json(200, {
                "baselines": BASELINES,
                "cli": cli,
                "cli_error": cli_error,
                "godot": godot,
                "examples": sorted(
                    str(p.relative_to(ROOT)).replace("\\", "/")
                    for p in (ROOT / "student-sdk" / "examples").glob("*.py")
                ),
                "submissions": find_submissions(),
            })
            return
        self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:
        if self.path != "/api/launch":
            self._send(404, b"not found", "text/plain")
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length > 64_000:
                raise LauncherError("request too large")
            request = json.loads(self.rfile.read(length) or b"{}")
            action = request.get("action", "headless")
            handler = ACTIONS.get(action)
            if handler is None:
                raise LauncherError(f"unknown action '{action}'")
            self._json(200, {"ok": True, **handler(request)})
        except LauncherError as exc:
            self._json(200, {"ok": False, "error": str(exc)})
        except subprocess.TimeoutExpired:
            self._json(200, {"ok": False, "error": "the match timed out"})
        except Exception as exc:  # noqa: BLE001 - never take the server down
            self._json(200, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})


def main() -> int:
    port = DEFAULT_PORT
    if len(sys.argv) > 1:
        port = int(sys.argv[1])

    try:
        cli = find_cli()
        print(f"  engine   {cli}")
    except LauncherError as exc:
        print(f"  engine   NOT FOUND - {exc}")
    godot = find_godot()
    print(f"  viewer   {godot or 'not found (headless and replay still work)'}")

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}"
    print(f"\n  Soccer launcher on {url}\n  Ctrl-C to stop.\n")
    threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
