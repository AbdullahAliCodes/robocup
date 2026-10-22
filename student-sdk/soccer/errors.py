"""Exception types the SDK raises.

Student-facing errors carry actionable messages: the local validator exists so
that a submission fails on the student's machine with an explanation, rather
than on the server with a stack trace.
"""

from __future__ import annotations


class SoccerError(Exception):
    """Base class for everything this package raises."""


class SubmissionError(SoccerError):
    """A submission could not be loaded, or does not implement the interface."""


class PolicyError(SoccerError):
    """A controller raised while deciding.

    The runner converts these into deterministic penalties rather than letting
    them escape into the match loop, so a crashing team loses points instead of
    taking the worker down with it.
    """

    def __init__(self, team: int, original: BaseException, tick: int) -> None:
        self.team = team
        self.original = original
        self.tick = tick
        super().__init__(
            f"team {team} raised {type(original).__name__} at tick {tick}: {original}"
        )
