"""Local validation of a submission.

The point of this module is that an invalid submission fails on the student's
own machine, with an explanation, instead of failing on the course server with
a stack trace and a zero. It checks the things the server will check, in the
order they will be checked.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .actions import TeamAction
from .controller import TeamController
from .env import SoccerEnv
from .errors import SubmissionError
from .helpers import load_controller

#: Seeds every student can test against. Grading uses a separate hidden set.
PUBLIC_SEEDS = (1001, 1002, 1003, 1004, 1005)


@dataclass
class ValidationReport:
    """What the validator found. ``ok`` is what decides acceptance."""

    ok: bool = True
    controller: Optional[str] = None
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    stats: Dict[str, Any] = field(default_factory=dict)

    def fail(self, message: str) -> "ValidationReport":
        self.ok = False
        self.errors.append(message)
        return self

    def warn(self, message: str) -> "ValidationReport":
        self.warnings.append(message)
        return self

    def render(self) -> str:
        lines = [f"submission: {self.controller or 'unknown'}"]
        for key, value in self.stats.items():
            lines.append(f"  {key:<28} {value}")
        for warning in self.warnings:
            lines.append(f"  warning: {warning}")
        for error in self.errors:
            lines.append(f"  ERROR:   {error}")
        lines.append("  PASSED" if self.ok else "  FAILED")
        return "\n".join(lines)


def validate(
    spec: str,
    *,
    seeds: Any = PUBLIC_SEEDS,
    ticks: int = 400,
    opponent: str = "balanced",
    deadline_ms: float = 20.0,
) -> ValidationReport:
    """Runs the acceptance checks against a submission specification."""
    report = ValidationReport()

    # 1. It has to load and implement the interface at all.
    try:
        controller = load_controller(spec)
    except SubmissionError as exc:
        report.controller = spec
        return report.fail(str(exc))
    report.controller = controller.describe()

    # 2. The kickoff formation, if the team defines one. The engine moves an
    #    illegal spot to the nearest legal one rather than refusing it, so a
    #    corrected formation still plays — but a student should hear about it
    #    here rather than wonder later why their shape looks wrong.
    formation: Optional[List[Any]] = None
    probe = SoccerEnv(max_ticks=ticks)
    try:
        try:
            requested = controller.initial_formation(probe.field)
        except BaseException as exc:  # noqa: BLE001 - contain everything
            report.fail(f"initial_formation raised - {type(exc).__name__}: {exc}")
            requested = None
        if requested is not None:
            try:
                formation = [(float(x), float(y)) for x, y in requested]
            except (TypeError, ValueError) as exc:
                report.fail(
                    f"initial_formation must return one (x, y) pair per player - {exc}"
                )
            else:
                expected = probe.players_per_team
                if len(formation) != expected:
                    report.warn(
                        f"initial_formation returned {len(formation)} spots for "
                        f"{expected} players; the rest take the default formation"
                    )
                corrected = probe.set_formation(0, formation)
                if corrected:
                    report.warn(
                        f"{corrected} of {len(formation)} formation spots were outside "
                        f"the rules and were moved to the nearest legal position "
                        f"(inside the pitch, your own half, and outside the "
                        f"{probe.field.centre_circle_radius:.1f} unit centre circle)"
                    )
    finally:
        probe.close()

    # 3. It has to survive a short match against a reference opponent, once per
    #    public seed, without raising or timing out.
    total_errors = 0
    total_timeouts = 0
    total_missing = 0
    total_non_finite = 0
    slowest_ms = 0.0
    total_ms = 0.0
    decisions = 0

    for seed in seeds:
        env = SoccerEnv(max_ticks=ticks, per_decision_timeout_ms=deadline_ms)
        env.use_baseline(1, opponent)
        try:
            # Played from the team's own formation, so what the validator
            # measures is the match the server will run.
            if formation is not None:
                env.set_formation(0, formation)
            observations = env.reset(seed=seed)
            previous = TeamController.idle(observations["team_a"])
            while not env.finished:
                started = time.perf_counter()
                try:
                    action = controller.act(observations["team_a"])
                    error = None
                except BaseException as exc:  # noqa: BLE001 - contain everything
                    action, error = None, f"{type(exc).__name__}: {exc}"
                elapsed_ms = (time.perf_counter() - started) * 1000.0

                decisions += 1
                total_ms += elapsed_ms
                slowest_ms = max(slowest_ms, elapsed_ms)

                if error is not None:
                    total_errors += 1
                    if total_errors == 1:
                        report.fail(f"seed {seed}: act raised - {error}")
                    action = previous
                elif not isinstance(action, TeamAction) and not isinstance(action, dict):
                    total_errors += 1
                    report.fail(
                        f"seed {seed}: returned {type(action).__name__}, "
                        f"expected a TeamAction"
                    )
                    action = previous
                else:
                    previous = action

                if elapsed_ms > deadline_ms:
                    total_timeouts += 1

                env.record_decision(0, elapsed_ms, error)
                if env.finished:
                    break
                observations, _ = env.step({"team_a": action})

            result = env.result()
            validation = result["stats"][0]["validation"]
            total_missing += validation["missing_actions"]
            total_non_finite += validation["non_finite_values"]
            if validation["invalid_player_ids"]:
                report.fail(
                    f"seed {seed}: returned actions for players this team does not "
                    f"control ({validation['invalid_player_ids']} times)"
                )
            if validation["dropped_debug_payloads"]:
                report.warn(
                    "debug payload exceeded the size limit and was dropped; "
                    "it is ignored during grading anyway"
                )
        finally:
            env.close()

    mean_ms = total_ms / decisions if decisions else 0.0
    report.stats = {
        "matches played": len(tuple(seeds)),
        "decisions": decisions,
        "mean decision (ms)": f"{mean_ms:.3f}",
        "slowest decision (ms)": f"{slowest_ms:.3f}",
        "deadline (ms)": f"{deadline_ms:.1f}",
        "exceptions": total_errors,
        "over deadline": total_timeouts,
        "missing player actions": total_missing,
        "non-finite values": total_non_finite,
    }

    report.stats["formation"] = (
        "default" if formation is None else f"custom ({len(formation)} spots)"
    )

    # 4. Reliability warnings that do not fail the submission but will cost
    #    marks or throughput on the server.
    if total_timeouts:
        report.fail(
            f"{total_timeouts} of {decisions} decisions exceeded the {deadline_ms:.0f} ms "
            f"deadline (slowest {slowest_ms:.1f} ms). On the server these become "
            f"repeated actions and count against reliability."
        )
    elif slowest_ms > deadline_ms * 0.5:
        report.warn(
            f"the slowest decision took {slowest_ms:.1f} ms, over half the "
            f"{deadline_ms:.0f} ms deadline; there is little headroom on a loaded server"
        )
    if total_missing:
        report.warn(
            f"{total_missing} player-ticks had no action supplied and defaulted to "
            f"standing still; check every player is covered"
        )
    if total_non_finite:
        report.fail(
            f"{total_non_finite} actions contained NaN or infinity and were rejected"
        )
    return report
