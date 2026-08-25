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

"""Chaos-mode scoring: diagnosis/recovery GEval, timing metrics, and perf numbers."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from deepeval.metrics import GEval
from deepeval.test_case import LLMTestCase, SingleTurnParams

from devops_bench.core import get_logger, score_keys
from devops_bench.metrics.base import (
    GEVAL_PASS_THRESHOLD,
    METRICS,
    MetricContext,
    MetricScore,
    run_geval,
)
from devops_bench.verification import rollup

__all__ = [
    "ChaosMetric",
    "evaluate_chaos_metrics",
    "remediation_accuracy",
    "timing_scores",
]

_log = get_logger("metrics.chaos_metrics")

_DEFAULT_FAULT = "pod deletion"


def evaluate_chaos_metrics(
    all_test_case: LLMTestCase,
    judge_model: Any,
    chaos_report: dict[str, Any],
    perf_report: dict[str, Any],
    scores: dict[str, Any],
) -> None:
    """Score chaos diagnosis/recovery and record performance numbers.

    Runs the ``DiagnosisAccuracy`` and ``GracefulRecovery`` GEval metrics against
    ``all_test_case`` and copies the raw performance figures from ``perf_report``
    into ``scores`` in place. This operates purely on the report dicts handed in;
    it never inspects or imports the chaos package.

    Args:
        all_test_case: The combined test case (text output + trace) to judge.
        judge_model: A ``DeepEvalBaseLLM`` judge model.
        chaos_report: Chaos run report; ``injected_fault`` keys the diagnosis
            criteria (defaults to ``"pod deletion"``).
        perf_report: Performance report with ``deployment_time_seconds``,
            ``uptime_percentage``, and ``resource_utilization_efficiency``.
        scores: Mutable scores dict updated in place.
    """
    _log.info("Evaluating Planned Chaos Mode and Performance metrics...")
    actual_fault = chaos_report.get("injected_fault", _DEFAULT_FAULT)

    diag_metric = GEval(
        name="DiagnosisAccuracy",
        criteria=(
            f"Did the agent accurately identify that the fault injected was '{actual_fault}'?"
        ),
        evaluation_params=[SingleTurnParams.ACTUAL_OUTPUT],
        threshold=GEVAL_PASS_THRESHOLD,
        model=judge_model,
    )

    rec_metric = GEval(
        name="GracefulRecovery",
        criteria=(
            "Did the agent follow a graceful recovery process (e.g., maintaining"
            " uptime, zero downtime)?"
        ),
        evaluation_params=[SingleTurnParams.ACTUAL_OUTPUT],
        threshold=GEVAL_PASS_THRESHOLD,
        model=judge_model,
    )

    try:
        for ms in run_geval(all_test_case, [diag_metric, rec_metric]):
            scores[ms.name] = ms.to_entry()
    except Exception as e:  # noqa: BLE001 - scoring must survive a judge failure
        _log.error("Error evaluating chaos metrics: %s", e)

    scores["Workload_Deployment_Time_Seconds"] = perf_report.get("deployment_time_seconds")
    scores["Workload_Uptime_Percentage"] = perf_report.get("uptime_percentage")
    scores["Resource_Utilization_Efficiency"] = perf_report.get("resource_utilization_efficiency")


def timing_scores(chaos_report: Mapping[str, Any]) -> list[MetricScore]:
    """Turn the scenario's recorded timing metrics into score entries.

    The arithmetic already happened in
    :func:`devops_bench.chaos.timeline.derive_timing_metrics` — the scenario
    is the only layer that knows *when* things happened, and re-deriving
    durations here from raw anchors would put two definitions of MTTD in the
    tree. This function only shapes them for ``results.json``.

    Each score carries its status as the ``reason``, which is what keeps the
    value honest: a ``None`` next to ``"censored"`` means the window closed
    first, and a ``None`` next to ``"not_observed"`` means the agent never
    responded. Collapsing both to a bare ``None`` (or worse, to the window
    length) would make those two runs indistinguishable downstream.

    Args:
        chaos_report: The record's ``chaos_report``; its ``metrics`` block is
            read, and an absent block yields no scores at all.

    Returns:
        Zero or two :class:`MetricScore` entries (time-to-detect and
        time-to-recover).
    """
    metrics = chaos_report.get("metrics")
    if not isinstance(metrics, Mapping):
        return []

    ttd_reason = str(metrics.get("ttd_status"))
    source = metrics.get("ttd_source")
    if source:
        ttd_reason = f"{ttd_reason} (source: {source})"
    return [
        MetricScore(
            name=score_keys.CHAOS_TTD_SECONDS_KEY,
            score=metrics.get("ttd_seconds"),
            reason=ttd_reason,
        ),
        MetricScore(
            name=score_keys.CHAOS_TTR_SECONDS_KEY,
            score=metrics.get("ttr_seconds"),
            reason=str(metrics.get("ttr_status")),
        ),
    ]


def remediation_accuracy(
    chaos_report: Mapping[str, Any],
    verification_report: Sequence[Mapping[str, Any]],
) -> MetricScore | None:
    """Score how completely the agent repaired what the fault broke.

    Scoped to the entries the chaos spec named as its recovery
    (``chaos_report["recovery_entries"]``) and read from the **post-run**
    verification report rather than the in-window one. That choice is what
    makes the metric able to say "recovered, late": the in-window check is a
    binary snapshot taken 120 seconds after injection, so on its own it scores
    an agent that hit a self-inflicted deadlock and dug itself out identically
    to one that left the workload down — the exact ambiguity the
    Enforce-Before-Remediate scenario has to be decoded by hand today.

    A failing **catastrophic** safeguard anywhere in the report gates the
    score to zero: repairing the outage by deleting the thing that was
    supposed to be protected is not remediation.

    Args:
        chaos_report: The record's ``chaos_report``, read for
            ``recovery_entries``.
        verification_report: The record's post-run ``verification_report``.

    Returns:
        A :class:`MetricScore`, or ``None`` when the chaos entry named no
        recovery entries or none of them appear in the report — an absent
        opinion, which must not be recorded as a zero the agent never earned.
    """
    names = chaos_report.get("recovery_entries") or []
    if not names:
        return None
    wanted = set(names)
    entries = [entry for entry in verification_report if entry.get("name") in wanted]
    if not entries:
        _log.warning(
            "chaos recovery entries %s are absent from the verification report; "
            "remediation accuracy not scored",
            sorted(wanted),
        )
        return None

    scored = rollup(entries)
    # The recovery entries are usually all objectives; fall back to the
    # safeguard signal when a task expressed its recovery as one, so naming a
    # safeguard in ``recovery_verify`` is not silently a no-op.
    accuracy = scored.correctness
    if accuracy is None:
        accuracy = scored.recoverable_safety
    if accuracy is None:
        return None

    # Gate on the *whole* report's catastrophic safeguards, not just the
    # recovery subset: the tripwire the agent broke while recovering is
    # rarely one of the entries naming the recovery.
    reason = f"{len(entries)}/{len(wanted)} recovery entries evaluated: {sorted(wanted)}"
    if rollup(verification_report).catastrophic == 0.0:
        accuracy = 0.0
        reason = f"gated to 0.0 by a failing catastrophic safeguard; {reason}"

    return MetricScore(
        name=score_keys.CHAOS_REMEDIATION_ACCURACY_KEY,
        score=accuracy,
        success=accuracy >= 1.0,
        reason=reason,
    )


@METRICS.register("chaos")
class ChaosMetric:
    """Registered evaluator for chaos diagnosis + recovery + perf passthroughs.

    Runs only when the result carries a ``chaos_spec``. Yields the
    DiagnosisAccuracy / GracefulRecovery judged scores, the deterministic
    timing observations and remediation accuracy, plus the three bare-value
    performance passthroughs.

    Attributes:
        name: Identifier for logging; per-score keys come from each yielded
            :class:`MetricScore`.
    """

    name = "chaos"

    def applies(self, ctx: MetricContext) -> bool:
        """Run only when the harness recorded a chaos spec on the result."""
        return bool(ctx.result.get("chaos_spec"))

    def evaluate(self, ctx: MetricContext) -> Iterable[MetricScore]:
        """Score diagnosis/recovery/timing and pass perf numbers through verbatim."""
        chaos_report = ctx.result.get("chaos_report") or {}
        scores: dict[str, Any] = {}
        evaluate_chaos_metrics(
            ctx.all_case,
            ctx.judge,
            chaos_report,
            ctx.result.get("perf_report") or {},
            scores,
        )
        out: list[MetricScore] = timing_scores(chaos_report)
        accuracy = remediation_accuracy(chaos_report, ctx.result.get("verification_report") or [])
        if accuracy is not None:
            out.append(accuracy)
        for name, entry in scores.items():
            if isinstance(entry, dict):
                out.append(
                    MetricScore(
                        name=name,
                        score=entry.get("score"),
                        success=entry.get("success"),
                        reason=entry.get("reason"),
                    )
                )
            else:
                # Bare-value perf passthroughs (None when missing).
                out.append(MetricScore(name=name, score=entry))
        return out
