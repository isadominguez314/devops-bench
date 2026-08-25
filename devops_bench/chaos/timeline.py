# Copyright 2026 The Kubernetes Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Wall-clock anchors for a chaos run, and the timing metrics derived from them.

This module is deliberately free of I/O, Kubernetes, and threading — it takes
the timestamps the scenario recorded and reduces them to the per-run timing
signals (kubernetes-sigs/devops-bench#94 asks for MTTD / MTTR). The mean in
"MTT*" is taken across runs by the aggregation layer; a single run contributes
one **TTD** and one **TTR** observation, which is what these names say.

Every duration is anchored on :attr:`ChaosTimeline.injected_at` — the instant
the disruption became real — so a slow trigger or a slow target lookup is never
charged to the agent.

Censoring is the reason each metric carries a status alongside its value. An
observation window that closes before the agent recovers produces "we stopped
watching at 120s", not "recovery took 120s"; emitting the latter as a number
would drag every mean toward the window length and make a fast model
indistinguishable from a slow one. A censored or unobserved outcome therefore
carries ``None`` as its value and says so in its status.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal

__all__ = [
    "ChaosTimeline",
    "TTD_STATUSES",
    "TTR_STATUSES",
    "derive_timing_metrics",
]

#: Outcome vocabulary for time-to-detection.
#:
#: ``observed`` — the watcher saw the blast radius mutated; the value is real.
#: ``not_observed`` — the watcher ran to the end of its window and saw nothing,
#: i.e. the agent never touched the blast radius. Right-censored: no value.
#: ``unavailable`` — no watcher ran (the fault declares no blast radius, the
#: spec configured no ``detect`` node, or injection never happened).
#: ``skipped`` — the trigger never fired, so there was no fault to detect.
TTD_STATUSES = ("observed", "not_observed", "unavailable", "skipped")

#: Outcome vocabulary for time-to-recovery.
#:
#: ``recovered`` — the recovery check converged inside the window; value real.
#: ``censored`` — a converging check ran out of window while still failing. The
#: agent may have recovered a second later; the run simply stopped looking.
#: No value. The post-run verification pass is what distinguishes
#: "recovered late" from "never recovered" (see
#: :func:`devops_bench.metrics.chaos_metrics.evaluate_chaos_metrics`).
#: ``not_recovered`` — a single-shot (assert-mode) check observed the workload
#: still broken. A definite negative, not a censored one.
#: ``unavailable`` — no recovery check was scheduled, or it errored out.
#: ``skipped`` — the trigger never fired, so there was nothing to recover from.
TTR_STATUSES = ("recovered", "censored", "not_recovered", "unavailable", "skipped")


@dataclass
class ChaosTimeline:
    """Wall-clock anchors for one chaos run, in UTC epoch seconds.

    Every field is ``None`` until the moment it marks actually happens, so a
    run that crashes mid-scenario still serializes a partial timeline showing
    how far it got.

    Attributes:
        scenario_started_at: The scenario thread began waiting on the trigger.
        trigger_fired_at: The trigger's condition was satisfied. The gap to
            ``scenario_started_at`` is the trigger's own latency, which is
            attributable to the *task design* (or, for ``agent_action``, to
            the agent reaching the watched step) and never to detection.
        injected_at: The disruption became real. The anchor for every metric.
        reverted_at: A self-reverting fault undid its disruption.
        first_action_at: The detection watcher saw the blast radius mutated by
            something other than the fault.
        recovery_observed_at: The recovery check first evaluated true.
        agent_started_at: The operator agent under test was launched.
        agent_finished_at: The operator agent returned or died.
    """

    scenario_started_at: float | None = None
    trigger_fired_at: float | None = None
    injected_at: float | None = None
    reverted_at: float | None = None
    first_action_at: float | None = None
    recovery_observed_at: float | None = None
    agent_started_at: float | None = None
    agent_finished_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-serializable mapping written onto ``chaos_report``."""
        return asdict(self)


def _elapsed(start: float | None, end: float | None) -> float | None:
    """Return ``end - start`` in seconds, or ``None`` when unanchored.

    A negative span is clamped to ``0.0`` rather than dropped: it means the
    watcher's baseline poll raced the injection by a few milliseconds, and the
    honest reading of that is "immediately", not "unmeasurable".
    """
    if start is None or end is None:
        return None
    return max(0.0, end - start)


def derive_timing_metrics(
    timeline: ChaosTimeline,
    *,
    injected: bool,
    detection_configured: bool,
    detection_fired: bool,
    recovery_status: Literal["pass", "fail", "error"] | None = None,
    recovery_mode: str | None = None,
) -> dict[str, Any]:
    """Reduce a timeline plus the run's outcome flags to the timing metrics.

    Args:
        timeline: The anchors the scenario recorded.
        injected: Whether the fault was actually injected. False covers both
            "the trigger never fired" and "injection failed"; the two are
            told apart by whether ``timeline.trigger_fired_at`` is set.
        detection_configured: Whether a detection watcher was started at all.
        detection_fired: Whether that watcher observed the blast radius change
            before its window closed.
        recovery_status: Tri-state outcome of the in-window recovery check
            (``VerificationResult.status``), or ``None`` when none was
            scheduled.
        recovery_mode: The recovery entry's resolved mode (``"converge"`` /
            ``"assert"``). A failing converge check is *censored*; a failing
            assert check is a definite negative.

    Returns:
        A mapping carrying ``ttd_seconds`` / ``ttd_status`` / ``ttd_source``,
        ``ttr_seconds`` / ``ttr_status``, and the two descriptive spans
        (``injection_duration_seconds``, ``trigger_latency_seconds``). Values
        are ``None`` wherever the corresponding status says no observation was
        made, so an unmeasured run never contributes a fabricated number.
    """
    skipped = not injected and timeline.trigger_fired_at is None

    if skipped:
        ttd_status, ttd_seconds = "skipped", None
    elif not injected or not detection_configured:
        ttd_status, ttd_seconds = "unavailable", None
    elif detection_fired:
        ttd_status = "observed"
        ttd_seconds = _elapsed(timeline.injected_at, timeline.first_action_at)
        # An anchor is missing (a fault that never stamped ``injected_at``):
        # the watcher fired but the span is unanchored, so report no number.
        if ttd_seconds is None:
            ttd_status = "unavailable"
    else:
        ttd_status, ttd_seconds = "not_observed", None

    if skipped:
        ttr_status, ttr_seconds = "skipped", None
    elif not injected or recovery_status is None:
        ttr_status, ttr_seconds = "unavailable", None
    elif recovery_status == "pass":
        ttr_status = "recovered"
        ttr_seconds = _elapsed(timeline.injected_at, timeline.recovery_observed_at)
        if ttr_seconds is None:
            ttr_status = "unavailable"
    elif recovery_status == "error":
        ttr_status, ttr_seconds = "unavailable", None
    elif recovery_mode == "assert":
        ttr_status, ttr_seconds = "not_recovered", None
    else:
        ttr_status, ttr_seconds = "censored", None

    return {
        "ttd_seconds": ttd_seconds,
        "ttd_status": ttd_status,
        # Provenance travels with the number: a later trajectory-timestamp
        # implementation writes a different source under the same key, and a
        # reader must never average the two as though they measured the same
        # thing.
        "ttd_source": "cluster_action" if ttd_status == "observed" else None,
        "ttr_seconds": ttr_seconds,
        "ttr_status": ttr_status,
        "injection_duration_seconds": _elapsed(timeline.injected_at, timeline.reverted_at),
        "trigger_latency_seconds": _elapsed(
            timeline.scenario_started_at, timeline.trigger_fired_at
        ),
    }
