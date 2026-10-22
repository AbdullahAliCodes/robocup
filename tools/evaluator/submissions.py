"""Finding, checking and identifying student submissions.

A submissions root holds one folder per team::

    submissions/
      2412345-alice-and-bob/
        team.py      required   all of the team's code, in one file
        team.toml    optional   team name and members, written by the intake
        README.md    optional   anything the marker should read
        data/        optional   read-only data files, up to DATA_LIMIT_BYTES

The folder name is the team's slug: it identifies the team in every result row,
replay file name and web page, so it is checked and never derived from anything
a student writes inside a file.

**One file is the whole submission.** Students hand in ``team.py`` through
Moodle and nothing else — Moodle already knows who they are, so asking them to
restate it in a file bought nothing and cost a class's worth of typos. The
intake writes ``team.toml`` from that identity (see
``Arena.add_submission``); when a folder has none, the team is identified and
displayed by its slug.

**One file of code, deliberately.** ``team.py`` is imported directly by path,
not as a package, so a second module next to it would not be importable and a
submission that splits itself across files would fail at load time on the
server. Detecting that here — before the tournament — turns a zero into a
fixable error message. It also means two submissions can never collide over a
module name, however many run in the same worker process.

Nothing in this module executes submission code. That happens in
:func:`check_behaviour`, which runs ``soccer validate --json`` in a subprocess
with a timeout, because a submission that hangs on import must not be able to
hang the marker.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .metadata import (
    METADATA_FILE,
    TEAM_MODULE,
    MetadataError,
    TeamMetadata,
    load_metadata,
    scaffold_files,
)

#: Slugs travel into file names, JSON keys and URLs, so they are kept boring.
SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}$")

#: A team file is code, not data. Anything this size is a mistake or a dump.
CODE_LIMIT_BYTES = 256 * 1024
#: Trained weights and lookup tables are legitimate; a dataset is not.
DATA_LIMIT_BYTES = 5 * 1024 * 1024

#: Extra top-level names that are allowed and simply ignored.
ALLOWED_EXTRAS = {
    "readme.md", "readme.txt", "notes.md", ".gitignore", "data",
}
#: Names not worth mentioning at all.
IGNORED_EXTRAS = {"__pycache__", ".git", ".ds_store", "desktop.ini", ".ipynb_checkpoints"}

#: Imports that a submission has no business making. Flagged, not blocked: the
#: platform has no sandbox yet, so a human decides, and the flag is what tells
#: them where to look. See docs/platform.md, "Not yet built".
SUSPICIOUS_IMPORTS = (
    "subprocess", "socket", "ctypes", "multiprocessing", "threading", "shutil",
    "urllib", "requests", "http.client", "ftplib", "pickle", "marshal",
    "importlib", "pty", "signal", "resource", "webbrowser",
)
SUSPICIOUS_CALLS = ("eval(", "exec(", "__import__(", "os.system", "os.popen", "os.remove")


class SubmissionError(Exception):
    """A problem with the submissions root itself, not with one submission."""


@dataclass
class Submission:
    """One team's folder, with everything discovery could establish about it."""

    slug: str
    path: Path
    metadata: Optional[TeamMetadata] = None
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    #: Static review notes for staff: what the code imports that it should not.
    flags: List[str] = field(default_factory=list)
    #: Filled in by check_behaviour().
    validation: Optional[Dict[str, Any]] = None
    #: Set by entrants() when the course names this slug as staff rather than
    #: a student to be ranked. Never derived from anything the submission
    #: itself claims, the same way a baseline is named on the command line and
    #: not inferred from a folder's contents — a submission cannot promote
    #: itself out of the students' table.
    role: str = ""

    # -- identity ---------------------------------------------------------

    @property
    def ok(self) -> bool:
        """True when this submission may be entered into a tournament."""
        return not self.errors and (self.validation is None or self.validation.get("ok"))

    @property
    def kind(self) -> str:
        return self.role or "submission"

    @property
    def name(self) -> str:
        """The display name. Falls back to the slug for a broken submission."""
        return self.metadata.name if self.metadata else self.slug

    @property
    def version(self) -> str:
        return self.metadata.version if self.metadata else "0"

    @property
    def team_file(self) -> Path:
        return self.path / TEAM_MODULE

    @property
    def spec(self) -> str:
        """What ``soccer.helpers.load_controller`` is given for this team.

        An absolute path, because the worker process that loads it has no reason
        to share a working directory with the marker.
        """
        entry_class = self.metadata.entry_class if self.metadata else ""
        spec = str(self.team_file)
        return f"{spec}:{entry_class}" if entry_class else spec

    @property
    def members(self) -> List[Dict[str, str]]:
        return list(self.metadata.members) if self.metadata else []

    def as_dict(self) -> Dict[str, Any]:
        """The row shape the web pages and the run manifest both use."""
        return {
            "slug": self.slug,
            "name": self.name,
            "version": self.version,
            "kind": self.kind,
            "members": self.members,
            "ok": self.ok,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "flags": list(self.flags),
            "validation": self.validation,
        }


@dataclass
class Baseline:
    """A reference team entered alongside the students.

    Included so a table has an absolute yardstick on it: "beat ``balanced``" is
    a claim a student can act on, where "eleventh of thirty" is not.
    """

    slug: str

    @property
    def name(self) -> str:
        return self.slug

    @property
    def version(self) -> str:
        return "engine"

    @property
    def kind(self) -> str:
        return "baseline"

    @property
    def spec(self) -> str:
        return self.slug

    @property
    def ok(self) -> bool:
        return True

    @property
    def members(self) -> List[Dict[str, str]]:
        return []

    def as_dict(self) -> Dict[str, Any]:
        return {
            "slug": self.slug,
            "name": self.slug,
            "version": "engine",
            "kind": "baseline",
            "members": [],
            "ok": True,
            "errors": [],
            "warnings": [],
            "flags": [],
            "validation": None,
        }


#: An entrant is a student submission or a built-in baseline. Both expose
#: ``slug``, ``name``, ``version``, ``spec`` and ``as_dict``, which is all the
#: tournament needs to know about either.
Entrant = Any


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def slug_for(directory_name: str) -> str:
    """The slug a folder name maps to.

    Lowercased, with anything outside the safe set collapsed to a dash, so that
    ``Alice & Bob (final)`` becomes ``alice-bob-final`` rather than being
    rejected outright. Collisions after collapsing are an error, not a silent
    merge — see :func:`discover`.
    """
    lowered = directory_name.strip().lower()
    collapsed = re.sub(r"[^a-z0-9._-]+", "-", lowered).strip("-._")
    return collapsed or "team"


def discover(root: Path, *, static_review: bool = True) -> List[Submission]:
    """Reads every subfolder of ``root`` as a submission.

    Returns submissions in slug order, valid and invalid alike: a run manifest
    records who was rejected and why, which is what a student asks about.
    """
    root = Path(root)
    if not root.is_dir():
        raise SubmissionError(f"no submissions directory at {root}")

    submissions: List[Submission] = []
    by_slug: Dict[str, str] = {}
    for entry in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        if not entry.is_dir() or entry.name.lower() in IGNORED_EXTRAS:
            continue
        submission = inspect(entry, static_review=static_review)
        clash = by_slug.get(submission.slug)
        if clash is not None:
            submission.errors.append(
                f"folder name collides with '{clash}': both are identified as "
                f"'{submission.slug}'. Rename one of them."
            )
        else:
            by_slug[submission.slug] = entry.name
        submissions.append(submission)

    if not submissions:
        raise SubmissionError(
            f"{root} contains no submission folders. Each team needs its own "
            f"folder with {TEAM_MODULE} in it."
        )
    return sorted(submissions, key=lambda s: s.slug)


def inspect(directory: Path, *, static_review: bool = True) -> Submission:
    """Checks one submission folder's structure and metadata."""
    submission = Submission(slug=slug_for(directory.name), path=Path(directory))
    if not SLUG_PATTERN.match(submission.slug):
        submission.errors.append(
            f"folder name {directory.name!r} does not reduce to a usable "
            f"identifier; use 2-64 characters of letters, digits, dot, dash or "
            f"underscore"
        )

    _check_required_files(submission)
    _check_extras(submission)
    if static_review and submission.team_file.is_file():
        submission.flags.extend(review_source(submission.team_file))
    return submission


def _check_required_files(submission: Submission) -> None:
    team_file = submission.team_file
    if not team_file.is_file():
        submission.errors.append(
            f"missing {TEAM_MODULE}. All of your code goes in a file with that "
            f"exact name, in this folder."
        )
    elif team_file.stat().st_size == 0:
        submission.errors.append(f"{TEAM_MODULE} is empty")
    elif team_file.stat().st_size > CODE_LIMIT_BYTES:
        submission.errors.append(
            f"{TEAM_MODULE} is {team_file.stat().st_size // 1024} KB, over the "
            f"{CODE_LIMIT_BYTES // 1024} KB limit for code. Large tables belong "
            f"in data/."
        )

    # Optional, and deliberately so: a submission is one file. A folder without
    # metadata is identified by its slug, which is where every result row, page
    # and replay name takes the team's identity from anyway.
    metadata_file = submission.path / METADATA_FILE
    if not metadata_file.is_file():
        return
    try:
        text = metadata_file.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        submission.errors.append(f"{METADATA_FILE} is not valid UTF-8 text")
        return
    try:
        submission.metadata = load_metadata(text)
    except MetadataError as exc:
        submission.errors.append(f"{METADATA_FILE}: {exc}")


def _check_extras(submission: Submission) -> None:
    """Reports anything in the folder that will not be part of the submission."""
    for entry in sorted(submission.path.iterdir(), key=lambda p: p.name.lower()):
        lowered = entry.name.lower()
        if lowered in (TEAM_MODULE, METADATA_FILE) or lowered in IGNORED_EXTRAS:
            continue
        if entry.is_symlink():
            submission.errors.append(f"{entry.name} is a symlink; submit real files")
            continue
        if lowered.endswith(".py"):
            # This is an error and not a warning because the student's own
            # machine will import it happily while the server will not: team.py
            # is loaded by path, so `import helpers` next to it fails there.
            submission.errors.append(
                f"{entry.name}: a submission is one file. Move everything into "
                f"{TEAM_MODULE} - extra modules next to it are not importable "
                f"when the server loads your team."
            )
            continue
        if lowered == "data" and entry.is_dir():
            _check_data_directory(submission, entry)
            continue
        if lowered in ALLOWED_EXTRAS:
            continue
        submission.warnings.append(f"{entry.name} is ignored; it is not part of your submission")


def _check_data_directory(submission: Submission, directory: Path) -> None:
    total = 0
    for path in directory.rglob("*"):
        if path.is_symlink():
            submission.errors.append(f"data/{path.name} is a symlink; submit real files")
            continue
        if path.is_dir():
            continue
        if path.suffix == ".py":
            submission.warnings.append(
                f"data/{path.name} will not be imported; data/ is for data files"
            )
        total += path.stat().st_size
    if total > DATA_LIMIT_BYTES:
        submission.errors.append(
            f"data/ holds {total // (1024 * 1024)} MB, over the "
            f"{DATA_LIMIT_BYTES // (1024 * 1024)} MB limit"
        )


def review_source(path: Path) -> List[str]:
    """Notes on what a team file reaches for, for a human to read.

    Not a security control — a determined submission gets around a text scan
    trivially. It is a triage aid: with no sandbox in place, staff need to know
    which of thirty submissions to actually read.
    """
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return [f"could not read {path.name}: {exc}"]

    flags: List[str] = []
    for line_number, line in enumerate(source.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        for module in SUSPICIOUS_IMPORTS:
            if re.search(rf"^\s*(import|from)\s+{re.escape(module)}\b", line):
                flags.append(f"line {line_number}: imports {module}")
        for call in SUSPICIOUS_CALLS:
            if call in stripped:
                flags.append(f"line {line_number}: calls {call.rstrip('(')}")
        if re.search(r"open\s*\([^)]*['\"][wax]", stripped):
            flags.append(f"line {line_number}: opens a file for writing")
    return flags


# ---------------------------------------------------------------------------
# Does it actually run?
# ---------------------------------------------------------------------------


def check_behaviour(
    submission: Submission,
    *,
    root: Path,
    opponent: str = "balanced",
    ticks: int = 400,
    deadline_ms: float = 20.0,
    timeout_secs: float = 180.0,
    python: Optional[str] = None,
) -> Dict[str, Any]:
    """Runs the student-facing validator against a submission, out of process.

    The same check the student runs locally with ``soccer validate``, so a
    submission that passes there passes here. Out of process because a
    submission that loops for ever on import would otherwise wedge the marker,
    and because a syntax error should cost one subprocess rather than a run.
    """
    if submission.errors:
        submission.validation = {
            "ok": False,
            "errors": ["not run: the submission's structure is invalid"],
            "warnings": [],
            "stats": {},
            "controller": None,
        }
        return submission.validation

    command = [
        python or sys.executable, "-m", "soccer.cli", "validate", submission.spec,
        "--against", opponent, "--ticks", str(ticks),
        "--deadline-ms", str(deadline_ms), "--json",
    ]
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(Path(root) / "student-sdk"), environment.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    # Leave the submission folder exactly as it was handed in: no __pycache__,
    # and no write attempt if it happens to be on a read-only mount.
    environment["PYTHONDONTWRITEBYTECODE"] = "1"

    try:
        completed = subprocess.run(
            command, cwd=str(root), capture_output=True, text=True,
            timeout=timeout_secs, env=environment,
        )
    except subprocess.TimeoutExpired:
        submission.validation = {
            "ok": False,
            "errors": [
                f"validation did not finish within {timeout_secs:.0f} s. A team "
                f"that hangs cannot be evaluated: check for an unbounded loop."
            ],
            "warnings": [],
            "stats": {},
            "controller": None,
        }
        return submission.validation

    report = _parse_report(completed)
    submission.validation = report
    submission.warnings.extend(report.get("warnings", []))
    return report


def _parse_report(completed: "subprocess.CompletedProcess[str]") -> Dict[str, Any]:
    for line in reversed((completed.stdout or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict) and "ok" in payload:
                return payload
    detail = (completed.stderr or completed.stdout or "").strip().splitlines()
    return {
        "ok": False,
        "errors": [detail[-1] if detail else "the validator produced no report"],
        "warnings": [],
        "stats": {},
        "controller": None,
    }


def check_all(
    submissions: Sequence[Submission],
    *,
    root: Path,
    progress=None,
    **kwargs: Any,
) -> None:
    """Validates every submission in turn, reporting progress as it goes."""
    for index, submission in enumerate(submissions, start=1):
        if progress is not None:
            progress(index, len(submissions), submission)
        check_behaviour(submission, root=root, **kwargs)


# ---------------------------------------------------------------------------
# Entrants
# ---------------------------------------------------------------------------


#: Course staff whose submissions play the field for comparison but are not
#: ranked as a student's own work. Matched by slug, the same way a baseline is
#: matched by name — see ``entrants()``. Named here rather than left to be
#: guessed from a folder, the way ``--baseline`` is a name on the command line
#: and not something a submission can claim about itself.
DEFAULT_LECTURERS: Tuple[str, ...] = (
    "73914-branden-ingram",
    "74118-devon-jarvis",
    "74607-pravesh-ranchod",
)


def entrants(
    submissions: Iterable[Submission],
    baselines: Iterable[str] = (),
    lecturers: Iterable[str] = DEFAULT_LECTURERS,
) -> List[Entrant]:
    """The field for a tournament: valid submissions plus named baselines.

    Slugs are unique across the field, so a submission folder called
    ``balanced`` cannot quietly displace the baseline of that name.

    ``lecturers`` tags submissions from ``slug_for(name) in lecturers`` as
    ``kind="lecturer"`` instead of ``"submission"``. They still play every
    fixture a student's team would — so "how do I do against a lecturer's
    team?" is still answerable — but ``tournament.divisions`` leaves them
    unbanded, the same way it leaves a baseline unbanded.
    """
    lecturer_slugs = {slug_for(name) for name in lecturers}
    field_list: List[Entrant] = []
    taken: Dict[str, str] = {}
    for submission in submissions:
        if not submission.ok:
            continue
        if submission.slug in lecturer_slugs:
            submission.role = "lecturer"
        taken[submission.slug] = "submission"
        field_list.append(submission)
    for name in baselines:
        slug = name if name not in taken else f"baseline-{name}"
        baseline = Baseline(slug=slug)
        if slug in taken:
            continue
        taken[slug] = "baseline"
        field_list.append(baseline)
    return field_list


def scaffold(directory: Path) -> List[Path]:
    """Writes a valid empty submission into ``directory``. Never overwrites."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    written: List[Path] = []
    for relative, contents in scaffold_files(slug_for(directory.name)):
        target = directory / relative
        if target.exists():
            continue
        target.write_text(contents, encoding="utf-8")
        written.append(target)
    return written
