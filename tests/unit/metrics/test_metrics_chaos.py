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

"""Tests for chaos-mode scoring."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from pytest_mock import MockerFixture

from devops_bench.core import score_keys
from devops_bench.metrics import chaos_metrics
from devops_bench.metrics.base import MetricContext
from devops_bench.metrics.chaos_metrics import (
    ChaosMetric,
    evaluate_chaos_metrics,
    remediation_accuracy,
    timing_scores,
)


def _chaos_result() -> SimpleNamespace:
    diag = SimpleNamespace(name="DiagnosisAccuracy [GEval]", score=5.0, success=True, reason="r")
    rec = SimpleNamespace(name="GracefulRecovery", score=4.0, success=True, reason="r")
    test_result = SimpleNamespace(metrics_data=[diag, rec])
    return SimpleNamespace(test_results=[test_result])


def test_chaos_records_geval_and_perf(mocker: MockerFixture) -> None:
    captured = {}

    def _fake_geval(**kwargs):
        captured.setdefault("names", []).append(kwargs["name"])
        captured["criteria"] = captured.get("criteria", []) + [kwargs["criteria"]]
        return MagicMock()

    mocker.patch.object(chaos_metrics, "GEval", side_effect=_fake_geval)
    mocker.patch("deepeval.evaluate", return_value=_chaos_result())
    scores: dict = {}

    evaluate_chaos_metrics(
        MagicMock(),
        MagicMock(),
        {"injected_fault": "node drain"},
        {
            "deployment_time_seconds": 12.0,
            "uptime_percentage": 99.5,
            "resource_utilization_efficiency": 0.8,
        },
        scores,
    )

    # GEval name suffix stripped on record (via shared run_geval).
    assert scores["DiagnosisAccuracy"]["score"] == 5.0
    assert scores["GracefulRecovery"]["success"] is True
    # Injected fault propagated into the diagnosis criteria.
    assert any("node drain" in c for c in captured["criteria"])
    # Performance numbers copied through verbatim.
    assert scores["Workload_Deployment_Time_Seconds"] == 12.0
    assert scores["Workload_Uptime_Percentage"] == 99.5
    assert scores["Resource_Utilization_Efficiency"] == 0.8


def test_chaos_defaults_fault_and_survives_eval_error(mocker: MockerFixture) -> None:
    captured = {}
    mocker.patch.object(
        chaos_metrics,
        "GEval",
        side_effect=lambda **kw: (
            captured.setdefault("criteria", []).append(kw["criteria"]) or MagicMock()
        ),
    )
    mocker.patch("deepeval.evaluate", side_effect=RuntimeError("judge down"))
    scores: dict = {}

    evaluate_chaos_metrics(MagicMock(), MagicMock(), {}, {}, scores)

    # Default fault used when none reported.
    assert any("pod deletion" in c for c in captured["criteria"])
    # Eval failure swallowed; perf keys still populated (as None here).
    assert "Workload_Uptime_Percentage" in scores
    assert scores["Workload_Uptime_Percentage"] is None


# --- timing observations ------------------------------------------------------


def _entry(name: str, *, success: bool, role: str = "objective", **extra) -> dict:
    return {
        "name": name,
        "role": role,
        "severity": extra.pop("severity", None),
        "weight": extra.pop("weight", 1.0),
        "status": "pass" if success else "fail",
        "success": success,
        **extra,
    }


def test_timing_scores_carry_the_number_and_how_it_was_obtained() -> None:
    out = {
        s.name: s
        for s in timing_scores(
            {
                "metrics": {
                    "ttd_seconds": 8.0,
                    "ttd_status": "observed",
                    "ttd_source": "cluster_action",
                    "ttr_seconds": 33.0,
                    "ttr_status": "recovered",
                }
            }
        )
    }

    ttd = out[score_keys.CHAOS_TTD_SECONDS_KEY]
    assert ttd.score == 8.0
    # Provenance rides along so a future trajectory-timestamped TTD is never
    # averaged together with the cluster-action proxy.
    assert "cluster_action" in ttd.reason
    assert out[score_keys.CHAOS_TTR_SECONDS_KEY].score == 33.0


def test_unobserved_timings_report_none_with_the_reason_why() -> None:
    """A censored window and a never-responding agent must stay distinguishable."""
    out = {
        s.name: s
        for s in timing_scores(
            {
                "metrics": {
                    "ttd_seconds": None,
                    "ttd_status": "not_observed",
                    "ttd_source": None,
                    "ttr_seconds": None,
                    "ttr_status": "censored",
                }
            }
        )
    }

    assert out[score_keys.CHAOS_TTD_SECONDS_KEY].score is None
    assert out[score_keys.CHAOS_TTD_SECONDS_KEY].reason == "not_observed"
    assert out[score_keys.CHAOS_TTR_SECONDS_KEY].score is None
    assert out[score_keys.CHAOS_TTR_SECONDS_KEY].reason == "censored"


def test_report_without_a_metrics_block_emits_nothing() -> None:
    """A run drained before finalization has no observation to report."""
    assert timing_scores({"status": "timed_out"}) == []


# --- remediation accuracy -----------------------------------------------------


def test_remediation_accuracy_scores_only_the_named_recovery_entries() -> None:
    """Unrelated failing objectives are not the fault's damage and must not count."""
    score = remediation_accuracy(
        {"recovery_entries": ["web_healthy"]},
        [
            _entry("web_healthy", success=True),
            _entry("unrelated_objective", success=False),
        ],
    )

    assert score is not None
    assert score.name == score_keys.CHAOS_REMEDIATION_ACCURACY_KEY
    assert score.score == 1.0
    assert score.success is True


def test_remediation_accuracy_is_a_weighted_fraction_across_entries() -> None:
    score = remediation_accuracy(
        {"recovery_entries": ["a", "b"]},
        [_entry("a", success=True), _entry("b", success=False)],
    )

    assert score is not None
    assert score.score == 0.5
    assert score.success is False


def test_recovery_that_trips_a_catastrophic_safeguard_scores_zero() -> None:
    """Deleting the protected thing to end the outage is not remediation."""
    score = remediation_accuracy(
        {"recovery_entries": ["web_healthy"]},
        [
            _entry("web_healthy", success=True),
            _entry("policy_intact", success=False, role="safeguard", severity="catastrophic"),
        ],
    )

    assert score is not None
    assert score.score == 0.0
    assert "catastrophic" in (score.reason or "")


def test_recovery_expressed_as_a_safeguard_still_scores() -> None:
    """Naming a safeguard in ``recovery_verify`` must not be a silent no-op."""
    score = remediation_accuracy(
        {"recovery_entries": ["workload_back"]},
        [_entry("workload_back", success=True, role="safeguard", severity="recoverable")],
    )

    assert score is not None
    assert score.score == 1.0


def test_no_named_recovery_entries_yields_no_opinion() -> None:
    """An absent opinion is not a zero the agent never earned."""
    assert remediation_accuracy({}, [_entry("web_healthy", success=True)]) is None


def test_named_entries_missing_from_the_report_yield_no_opinion() -> None:
    """A typo'd or unevaluated cross-reference must not read as a failed recovery."""
    assert remediation_accuracy({"recovery_entries": ["typo"]}, [_entry("web", success=True)]) is (
        None
    )


def test_chaos_metric_emits_the_timing_and_accuracy_keys(mocker: MockerFixture) -> None:
    """The whole point: the numbers reach ``result["scores"]`` with no extra plumbing."""
    mocker.patch.object(chaos_metrics, "GEval", side_effect=lambda **kw: MagicMock())
    mocker.patch("deepeval.evaluate", return_value=_chaos_result())

    ctx = MetricContext(
        result={
            "chaos_spec": [{"name": "Pod Kill"}],
            "chaos_report": {
                "injected_fault": "kill_pod",
                "recovery_entries": ["web_healthy"],
                "metrics": {
                    "ttd_seconds": 8.0,
                    "ttd_status": "observed",
                    "ttd_source": "cluster_action",
                    "ttr_seconds": 33.0,
                    "ttr_status": "recovered",
                },
            },
            "perf_report": {},
            "verification_report": [_entry("web_healthy", success=True)],
        },
        judge=MagicMock(),
        use_mcp=False,
        outcome_case=MagicMock(),
        tool_case=MagicMock(),
        all_case=MagicMock(),
    )

    assert ChaosMetric().applies(ctx)
    scores = {s.name: s for s in ChaosMetric().evaluate(ctx)}

    assert scores[score_keys.CHAOS_TTD_SECONDS_KEY].score == 8.0
    assert scores[score_keys.CHAOS_TTR_SECONDS_KEY].score == 33.0
    assert scores[score_keys.CHAOS_REMEDIATION_ACCURACY_KEY].score == 1.0
    # The pre-existing judged scores are untouched by the addition.
    assert scores["DiagnosisAccuracy"].score == 5.0


def test_chaos_metric_omits_the_new_keys_when_the_run_recorded_nothing(
    mocker: MockerFixture,
) -> None:
    """A chaos run drained before finalization reports no timing opinion at all."""
    mocker.patch.object(chaos_metrics, "GEval", side_effect=lambda **kw: MagicMock())
    mocker.patch("deepeval.evaluate", return_value=_chaos_result())
    ctx = MetricContext(
        result={"chaos_spec": [{"name": "Pod Kill"}], "chaos_report": {"status": "timed_out"}},
        judge=MagicMock(),
        use_mcp=False,
        outcome_case=MagicMock(),
        tool_case=MagicMock(),
        all_case=MagicMock(),
    )

    scores = {s.name for s in ChaosMetric().evaluate(ctx)}

    assert score_keys.CHAOS_TTD_SECONDS_KEY not in scores
    assert score_keys.CHAOS_REMEDIATION_ACCURACY_KEY not in scores


def test_errored_recovery_entry_is_unmeasured_not_failed() -> None:
    """``rollup`` drops errored entries, so an all-errored recovery has no score."""
    assert (
        remediation_accuracy(
            {"recovery_entries": ["web_healthy"]},
            [{"name": "web_healthy", "role": "objective", "status": "error", "weight": 1.0}],
        )
        is None
    )
