"""Loading submissions and a few tactical conveniences."""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import sys
from pathlib import Path
from typing import Optional

from .controller import TeamController
from .errors import SubmissionError


def load_controller(spec: str) -> TeamController:
    """Instantiates a controller from a specification string.

    Accepted forms::

        path/to/team.py            the only TeamController subclass in the file
        path/to/team.py:MyTeam     a named class in a file
        package.module:MyTeam      a named class in an importable module
        package.module             the only TeamController subclass in a module

    Raises :class:`SubmissionError` with an actionable message on failure, so a
    student sees the problem locally rather than as a server-side stack trace.
    """
    target, class_name = _split_spec(spec)
    module = _import_module(target)
    controller_class = _select_class(module, class_name, spec)

    try:
        instance = controller_class()
    except TypeError as exc:
        raise SubmissionError(
            f"{controller_class.__name__} could not be constructed with no arguments: {exc}\n"
            "A submitted controller must be instantiable as MyTeam()."
        ) from exc

    if type(instance).act is TeamController.act:
        # A team written against the old two-phase interface loads fine and
        # then stands still for the whole match, which is a much harder thing
        # to work out from a result sheet than a message here.
        legacy = [
            name for name in ("attack", "defend") if hasattr(type(instance), name)
        ]
        if legacy:
            raise SubmissionError(
                f"{controller_class.__name__} defines "
                f"{' and '.join(f'{name}()' for name in legacy)} but not act(). "
                f"There are no separate attacking and defending methods any "
                f"more: write one act(self, observation) that decides for the "
                f"whole team, and work out for yourself whether the ball is "
                f"yours."
            )
        raise SubmissionError(
            f"{controller_class.__name__} does not implement act(). "
            f"A team must override act(self, observation)."
        )
    if instance.name == "unnamed_team":
        instance.name = controller_class.__name__
    return instance


def _split_spec(spec: str) -> tuple[str, Optional[str]]:
    """Splits ``target[:ClassName]``.

    Splitting on the last colon rather than the first, and only when what
    follows is a valid identifier, keeps Windows absolute paths intact:
    ``C:\\work\\team.py`` must not be read as module ``C`` with a class name of
    ``\\work\\team.py``.
    """
    target, separator, class_name = spec.rpartition(":")
    if not separator or not class_name.isidentifier():
        return spec, None
    return target, class_name


def _import_module(target: str):
    path = Path(target)
    if path.suffix == ".py":
        if not path.exists():
            raise SubmissionError(f"no such file: {path}")
        module_name = f"_soccer_submission_{path.stem}"
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise SubmissionError(f"cannot load {path} as a Python module")
        module = importlib.util.module_from_spec(spec)
        # Registering the module first lets the file use dataclasses and
        # relative-free imports that expect to find themselves in sys.modules.
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        except Exception as exc:
            raise SubmissionError(f"{path} raised while being imported: {exc}") from exc
        return module

    try:
        return importlib.import_module(target)
    except ImportError as exc:
        raise SubmissionError(
            f"cannot import '{target}'. Give a .py file path or an importable "
            f"module name, optionally followed by ':ClassName'."
        ) from exc


def _select_class(module, class_name: Optional[str], spec: str):
    if class_name:
        controller_class = getattr(module, class_name, None)
        if controller_class is None:
            raise SubmissionError(f"'{class_name}' was not found in {spec}")
        if not (
            inspect.isclass(controller_class)
            and issubclass(controller_class, TeamController)
        ):
            raise SubmissionError(
                f"'{class_name}' is not a TeamController subclass"
            )
        return controller_class

    candidates = [
        value
        for value in vars(module).values()
        if inspect.isclass(value)
        and issubclass(value, TeamController)
        and value is not TeamController
        and value.__module__ == module.__name__
    ]
    if not candidates:
        raise SubmissionError(
            f"no TeamController subclass found in {spec}. "
            f"Define one:  class MyTeam(TeamController): ..."
        )
    if len(candidates) > 1:
        names = ", ".join(sorted(c.__name__ for c in candidates))
        raise SubmissionError(
            f"{spec} defines several controllers ({names}). "
            f"Say which one, for example '{spec}:{candidates[0].__name__}'."
        )
    return candidates[0]


def closest_to_ball(observation) -> bool:
    """Whether one of your players is the nearest player to the ball.

    The cheapest reading of "is this ours to win": ``True`` when your nearest
    player is closer to the ball than any of theirs. It is a fact about
    distance and nothing else — it says nothing about who touched the ball
    last, and on an exact tie, or with a side missing players, it is ``False``.

    Possession is the other reading, and the two disagree often enough to be
    worth knowing apart: ``observation.ball.controlling_team == 0`` says whose
    ball it currently is, and persists while the ball runs loose, whereas this
    changes the moment somebody out-sprints you to it.
    """
    ball = observation.ball.position
    mine = observation.closest_my_player_to(ball)
    theirs = observation.closest_opponent_to(ball)
    if mine is None or theirs is None:
        return False

    def gap(player) -> float:
        dx = player.position[0] - ball[0]
        dy = player.position[1] - ball[1]
        return dx * dx + dy * dy

    return gap(mine) < gap(theirs)
