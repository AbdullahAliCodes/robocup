"""Vector helpers.

These are deliberately small and dependency-free so a first team can be written
without numpy. They are geometry and nothing else: the pieces you build a team
out of, not the team. Anything that has to agree with the engine exactly —
nearest player, kick timing, normalisation — is computed by the engine itself
on :class:`~soccer.observation.Observation` and is not reimplemented here.
"""

from __future__ import annotations

import math
from typing import NamedTuple, Sequence, Tuple, Union

#: A point or direction in field units. Anywhere the SDK accepts a vector it
#: also accepts a plain ``(x, y)`` tuple or list.
#:
#: Spelled with ``Union`` rather than ``X | Y``: this is a module-level
#: assignment, not an annotation, so ``from __future__ import annotations`` does
#: not defer it and the ``|`` form would require Python 3.10 at import time. The
#: compiled engine is built for the stable ABI from 3.8, and the SDK should not
#: be the thing that raises the floor.
VectorLike = Union[Tuple[float, float], Sequence[float]]


class Vec2(NamedTuple):
    """An immutable 2D vector that is also a plain ``(x, y)`` tuple."""

    x: float = 0.0
    y: float = 0.0

    def __add__(self, other: VectorLike) -> "Vec2":
        ox, oy = _pair(other)
        return Vec2(self.x + ox, self.y + oy)

    def __sub__(self, other: VectorLike) -> "Vec2":
        ox, oy = _pair(other)
        return Vec2(self.x - ox, self.y - oy)

    def __mul__(self, scalar: float) -> "Vec2":
        return Vec2(self.x * scalar, self.y * scalar)

    __rmul__ = __mul__

    def __truediv__(self, scalar: float) -> "Vec2":
        return Vec2(self.x / scalar, self.y / scalar)

    def __neg__(self) -> "Vec2":
        return Vec2(-self.x, -self.y)

    def length(self) -> float:
        return math.hypot(self.x, self.y)

    def length_squared(self) -> float:
        return self.x * self.x + self.y * self.y

    def dot(self, other: VectorLike) -> float:
        ox, oy = _pair(other)
        return self.x * ox + self.y * oy

    def normalised(self) -> "Vec2":
        return normalise(self)

    def perpendicular(self) -> "Vec2":
        return Vec2(-self.y, self.x)


def _pair(value: VectorLike) -> Tuple[float, float]:
    """Coerces anything vector-shaped into an ``(x, y)`` pair."""
    if isinstance(value, (tuple, list)):
        if len(value) < 2:
            raise ValueError(f"expected a 2D vector, got {value!r}")
        return float(value[0]), float(value[1])
    x = getattr(value, "x", None)
    y = getattr(value, "y", None)
    if x is None or y is None:
        raise ValueError(
            f"expected a 2D vector: an (x, y) tuple, an [x, y] list, or an object "
            f"with .x and .y, got {value!r}"
        )
    return float(x), float(y)


def normalise(value: VectorLike) -> Vec2:
    """Unit vector, or ``(0, 0)`` when the input is degenerate.

    Never returns NaN: the engine rejects non-finite actions, and silently
    producing one here would just cost a player their tick.
    """
    x, y = _pair(value)
    length = math.hypot(x, y)
    if length <= 1e-9 or not math.isfinite(length):
        return Vec2(0.0, 0.0)
    return Vec2(x / length, y / length)


def direction(origin: VectorLike, target: VectorLike) -> Vec2:
    """Unit vector pointing from ``origin`` towards ``target``."""
    ox, oy = _pair(origin)
    tx, ty = _pair(target)
    return normalise((tx - ox, ty - oy))


def distance(a: VectorLike, b: VectorLike) -> float:
    ax, ay = _pair(a)
    bx, by = _pair(b)
    return math.hypot(bx - ax, by - ay)


def scale_to_length(value: VectorLike, max_length: float) -> Vec2:
    """Shortens a vector to ``max_length``, leaving shorter ones untouched."""
    x, y = _pair(value)
    length = math.hypot(x, y)
    if length <= max_length or length <= 1e-9:
        return Vec2(x, y)
    return Vec2(x * max_length / length, y * max_length / length)


def clamp(value: float, low: float, high: float) -> float:
    return low if value < low else high if value > high else value


def lerp(a: VectorLike, b: VectorLike, t: float) -> Vec2:
    ax, ay = _pair(a)
    bx, by = _pair(b)
    return Vec2(ax + (bx - ax) * t, ay + (by - ay) * t)


def closest_point_on_segment(point: VectorLike, a: VectorLike, b: VectorLike) -> Vec2:
    """Closest point to ``point`` on the segment ``a``-``b``."""
    px, py = _pair(point)
    ax, ay = _pair(a)
    bx, by = _pair(b)
    dx, dy = bx - ax, by - ay
    length_sq = dx * dx + dy * dy
    if length_sq <= 1e-12:
        return Vec2(ax, ay)
    t = clamp(((px - ax) * dx + (py - ay) * dy) / length_sq, 0.0, 1.0)
    return Vec2(ax + dx * t, ay + dy * t)
