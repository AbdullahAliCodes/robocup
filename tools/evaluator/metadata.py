"""Reading and checking ``team.toml``.

A submission may carry machine-readable identity — who wrote it, what the team
is called — so that a mark and a leaderboard row can name people rather than a
folder. Students never write it: they hand in ``team.py`` through Moodle, which
already knows who they are, and the intake writes this file from that identity.
It is therefore optional on disk, and a folder without one is identified by its
slug; see :mod:`evaluator.submissions`.

TOML because the rest of the platform configures itself in TOML. ``tomllib``
arrived in Python 3.11 and the course machines are not all there yet, so this
module uses the standard parser when it exists, ``tomli`` if it is installed,
and otherwise falls back to a small parser covering exactly the subset a
metadata file needs: tables, arrays of tables, strings, integers, booleans and
single-line arrays. Anything more exotic in a submission's metadata is a
mistake worth reporting rather than a feature worth supporting.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple

#: The one place the required file names are written down.
TEAM_MODULE = "team.py"
METADATA_FILE = "team.toml"

#: Display names end up in result rows, replay headers and web pages. Keeping
#: them to this set means no escaping question anywhere downstream.
NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._'&()+-]{0,39}$")
STUDENT_NUMBER_PATTERN = re.compile(r"^[A-Za-z0-9]{5,12}$")
VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,15}$")
#: ``team.py`` or ``team.py:ClassName`` - never a path, never another file.
ENTRY_PATTERN = re.compile(r"^team\.py(?::[A-Za-z_][A-Za-z0-9_]*)?$")

MAX_MEMBERS = 6


class MetadataError(Exception):
    """A metadata file a student can fix, reported as a plain message."""


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def parse_toml(text: str) -> Dict[str, Any]:
    """Parses TOML with the best parser available."""
    try:
        import tomllib  # type: ignore[import-not-found]

        return tomllib.loads(text)
    except ImportError:
        pass
    try:
        import tomli  # type: ignore[import-not-found]

        return tomli.loads(text)
    except ImportError:
        pass
    return _parse_toml_subset(text)


def _parse_toml_subset(text: str) -> Dict[str, Any]:
    """A deliberately small TOML reader for metadata files.

    Supports ``[table]``, ``[[array.of.tables]]``, and ``key = value`` where a
    value is a quoted string, an integer, a float, a boolean or a single-line
    array of those. Errors carry the line number, because the whole point of
    validating locally is that the student can see what to fix.
    """
    document: Dict[str, Any] = {}
    target: Dict[str, Any] = document

    for number, raw in enumerate(text.splitlines(), start=1):
        line = _strip_comment(raw).strip()
        if not line:
            continue

        if line.startswith("[["):
            if not line.endswith("]]"):
                raise MetadataError(f"line {number}: unterminated [[table]] header")
            target = _open_array_table(document, line[2:-2].strip(), number)
            continue
        if line.startswith("["):
            if not line.endswith("]"):
                raise MetadataError(f"line {number}: unterminated [table] header")
            target = _open_table(document, line[1:-1].strip(), number)
            continue

        key, separator, value = line.partition("=")
        if not separator:
            raise MetadataError(f"line {number}: expected 'key = value', got {line!r}")
        key = key.strip().strip('"').strip("'")
        if not key:
            raise MetadataError(f"line {number}: missing key name")
        target[key] = _parse_value(value.strip(), number)

    return document


def _strip_comment(line: str) -> str:
    """Removes a trailing ``#`` comment, respecting quoted strings."""
    quote = ""
    for index, character in enumerate(line):
        if quote:
            if character == quote:
                quote = ""
        elif character in "\"'":
            quote = character
        elif character == "#":
            return line[:index]
    return line


def _path_parts(header: str, number: int) -> List[str]:
    parts = [part.strip().strip('"').strip("'") for part in header.split(".")]
    if not parts or any(not part for part in parts):
        raise MetadataError(f"line {number}: empty table name in [{header}]")
    return parts


def _open_table(document: Dict[str, Any], header: str, number: int) -> Dict[str, Any]:
    node: Any = document
    for part in _path_parts(header, number):
        if isinstance(node, list):
            node = node[-1]
        node = node.setdefault(part, {})
        if not isinstance(node, (dict, list)):
            raise MetadataError(f"line {number}: [{header}] redefines a value")
    return node[-1] if isinstance(node, list) else node


def _open_array_table(
    document: Dict[str, Any], header: str, number: int
) -> Dict[str, Any]:
    parts = _path_parts(header, number)
    node: Any = document
    for part in parts[:-1]:
        node = node.setdefault(part, {})
        if isinstance(node, list):
            node = node[-1]
    tables = node.setdefault(parts[-1], [])
    if not isinstance(tables, list):
        raise MetadataError(f"line {number}: [[{header}]] redefines a table")
    entry: Dict[str, Any] = {}
    tables.append(entry)
    return entry


def _parse_value(text: str, number: int) -> Any:
    if not text:
        raise MetadataError(f"line {number}: missing value")
    if text.startswith("["):
        if not text.endswith("]"):
            raise MetadataError(
                f"line {number}: an array must open and close on one line"
            )
        inner = text[1:-1].strip()
        if not inner:
            return []
        return [_parse_value(item.strip(), number) for item in _split_array(inner)]
    if text[0] in "\"'":
        quote = text[0]
        if len(text) < 2 or text[-1] != quote:
            raise MetadataError(f"line {number}: unterminated string {text!r}")
        body = text[1:-1]
        if quote == "'":
            return body  # literal string: no escapes, by definition
        return (
            body.replace("\\\\", "\x00")
            .replace('\\"', '"')
            .replace("\\n", "\n")
            .replace("\\t", "\t")
            .replace("\x00", "\\")
        )
    lowered = text.lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    try:
        return int(text.replace("_", ""))
    except ValueError:
        pass
    try:
        return float(text.replace("_", ""))
    except ValueError as exc:
        raise MetadataError(
            f"line {number}: {text!r} is not a string, number or boolean. "
            f"Quote it if it is text."
        ) from exc


def _split_array(inner: str) -> List[str]:
    """Splits array items on commas that are not inside a string."""
    items: List[str] = []
    quote = ""
    depth = 0
    current = ""
    for character in inner:
        if quote:
            current += character
            if character == quote:
                quote = ""
            continue
        if character in "\"'":
            quote = character
        elif character == "[":
            depth += 1
        elif character == "]":
            depth -= 1
        elif character == "," and depth == 0:
            items.append(current)
            current = ""
            continue
        current += character
    if current.strip():
        items.append(current)
    return items


# ---------------------------------------------------------------------------
# The schema
# ---------------------------------------------------------------------------


class TeamMetadata:
    """The validated contents of one ``team.toml``."""

    __slots__ = ("name", "version", "entry", "members", "notes")

    def __init__(
        self,
        name: str,
        version: str,
        entry: str,
        members: List[Dict[str, str]],
        notes: str = "",
    ) -> None:
        self.name = name
        self.version = version
        self.entry = entry
        self.members = members
        self.notes = notes

    @property
    def entry_class(self) -> str:
        """The class name the entry pins, or ``""`` for "the only one there"."""
        _, _, class_name = self.entry.partition(":")
        return class_name

    def as_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "entry": self.entry,
            "members": [dict(member) for member in self.members],
            "notes": self.notes,
        }

    def member_summary(self) -> str:
        return ", ".join(
            f"{member['name']} ({member['student_number']})" for member in self.members
        )


def load_metadata(text: str) -> TeamMetadata:
    """Parses and checks a metadata file, or raises :class:`MetadataError`."""
    document = parse_toml(text)
    team = document.get("team")
    if not isinstance(team, dict):
        raise MetadataError(
            "missing the [team] table. The file must contain at least:\n"
            '  [team]\n  name = "Your Team Name"\n\n  [[members]]\n'
            '  name = "Your Name"\n  student_number = "2412345"'
        )

    name = _require_string(team, "name", "team")
    if not NAME_PATTERN.match(name):
        raise MetadataError(
            f"team.name {name!r} is not usable as a display name. Use 1-40 "
            f"characters: letters, digits, spaces and . _ ' & ( ) + -"
        )

    version = str(team.get("version", "1")).strip() or "1"
    if not VERSION_PATTERN.match(version):
        raise MetadataError(
            f"team.version {version!r} must be 1-16 characters of letters, "
            f"digits, dots, dashes or underscores"
        )

    entry = str(team.get("entry", TEAM_MODULE)).strip() or TEAM_MODULE
    if not ENTRY_PATTERN.match(entry):
        raise MetadataError(
            f"team.entry {entry!r} must be 'team.py' or 'team.py:ClassName'. "
            f"All of a submission's code lives in {TEAM_MODULE}."
        )

    members = _load_members(document)
    notes = str(team.get("notes", "")).strip()[:500]
    return TeamMetadata(name=name, version=version, entry=entry, members=members, notes=notes)


def _load_members(document: Dict[str, Any]) -> List[Dict[str, str]]:
    raw = document.get("members")
    if isinstance(raw, dict):
        # [members] instead of [[members]]: a single member, written the wrong
        # way round. Accept it rather than failing a whole submission over a
        # pair of brackets.
        raw = [raw]
    if not isinstance(raw, list) or not raw:
        raise MetadataError(
            "no [[members]] entries. Add one per person:\n"
            '  [[members]]\n  name = "Your Name"\n  student_number = "2412345"'
        )
    if len(raw) > MAX_MEMBERS:
        raise MetadataError(f"at most {MAX_MEMBERS} members may be listed, found {len(raw)}")

    members: List[Dict[str, str]] = []
    seen: set = set()
    for index, entry in enumerate(raw, start=1):
        if not isinstance(entry, dict):
            raise MetadataError(f"member {index} is not a table")
        member_name = _require_string(entry, "name", f"members[{index}]")
        number = _require_string(entry, "student_number", f"members[{index}]")
        if not NAME_PATTERN.match(member_name):
            raise MetadataError(
                f"member {index}: name {member_name!r} contains characters that "
                f"cannot be displayed; use letters, digits, spaces and . _ ' -"
            )
        if not STUDENT_NUMBER_PATTERN.match(number):
            raise MetadataError(
                f"member {index}: student_number {number!r} does not look like a "
                f"student number (5-12 letters or digits, quoted as a string)"
            )
        if number.lower() in seen:
            raise MetadataError(f"student_number {number!r} is listed twice")
        seen.add(number.lower())
        members.append({"name": member_name, "student_number": number})
    return members


def _require_string(table: Dict[str, Any], key: str, where: str) -> str:
    value = table.get(key)
    if value is None:
        raise MetadataError(f"{where}.{key} is missing")
    if not isinstance(value, str):
        raise MetadataError(
            f"{where}.{key} must be a quoted string, got {type(value).__name__}"
        )
    return value.strip()


def template(slug: str = "your-team") -> str:
    """The metadata file the intake writes, with nothing filled in.

    Not student-facing: a submission is ``team.py`` alone, and this is what a
    staff-side import produces when it has no identity to put in it.
    """
    return (
        "# Identity for the marker and the leaderboard, written by the intake.\n"
        "[team]\n"
        f'name = "{slug.replace("-", " ").title()}"\n'
        'version = "1"          # bump when you change your tactics\n'
        "\n"
        "[[members]]\n"
        'name = "Your Name"\n'
        'student_number = "2412345"\n'
    )


#: The team a scaffolded submission starts as.
#:
#: Four branches and nothing else. Deliberately so: the engine reports where
#: everybody is and how the ball behaves, and turning that into football is the
#: coursework, so a scaffold that arrived with support positions and marking
#: already written would be handing back the part being marked. Everything
#: wrong with this team is wrong in a way a student can see and fix.
#:
#: Substituted with ``replace`` rather than ``format``: the code below has
#: braces in it that are Python, not placeholders.
STARTER_TEAM = '''"""Your team. Everything you write lives in this one file.

Every player asks two questions and does one of four things:

    is the ball ours?
        am I the closest of us to it?   ->  shoot at their goal
        otherwise                       ->  push up towards their goal
    otherwise
        am I the closest of us to it?   ->  go and get it
        otherwise                       ->  drop back towards our goal

That is the whole team, and it is a weak one. Nobody keeps goal, the players
who are not chasing all run to the same place, nobody passes, and every shot is
full power from wherever the player is standing. Fixing any of those is worth
more than tuning the rest.
"""

from soccer import PlayerAction, TeamAction, TeamController, direction


class MyTeam(TeamController):
    name = "__TEAM_NAME__"
    version = "1"

    def act(self, obs):
        """Called every tick, ball or no ball.

        `obs.ball.controlling_team == 0` is the engine's reading of possession
        and persists while the ball runs loose; `closest_to_ball(obs)`, from
        `soccer`, is the other way to ask and flips the moment somebody outruns
        you. They disagree often. Which one you build on is your decision, and
        so is whether to split the team in two at all.
        """
        actions = TeamAction()
        ours = obs.ball.controlling_team == 0
        chaser = obs.closest_my_player_to(obs.ball.position)

        for player in obs.my_players:
            nearest = player.id == chaser.id

            if ours and nearest:
                # Ours, and I am on it: have a go at goal. can_kick first, so
                # you are not spending ticks on kicks the engine rejects.
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
                # Ours, somebody else is on it: get up the pitch.
                actions.move(player.id, direction(player.position, obs.opponent_goal))
            elif nearest:
                # Theirs, and I am closest: go and win it back.
                actions.move(player.id, direction(player.position, obs.ball.position))
            else:
                # Theirs, somebody else is closer: get behind the ball.
                actions.move(player.id, direction(player.position, obs.field.my_goal))

        return actions
'''


def scaffold_files(slug: str) -> List[Tuple[str, str]]:
    """``(relative path, contents)`` for a fresh submission folder.

    One file, because that is what a submission is. The folder name carries the
    team's identity, so there is nothing else to write.
    """
    # The controller names itself after its folder. Two scaffolded teams played
    # against each other otherwise both report as "my_team", and the result
    # table cannot be read.
    identifier = re.sub(r"[^A-Za-z0-9_]", "_", slug).strip("_") or "my_team"
    return [
        (TEAM_MODULE, STARTER_TEAM.replace("__TEAM_NAME__", identifier)),
    ]
