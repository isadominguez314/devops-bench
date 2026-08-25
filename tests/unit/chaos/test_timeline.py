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

"""Tests for the chaos timing metrics derived from a run's wall-clock anchors."""

from __future__ import annotations

from devops_bench.chaos.timeline import (
    TTD_STATUSES,
    TTR_STATUSES,
    ChaosTimeline,
    derive_timing_metrics,
)


def _observed_timeline(**overrides: float | None) -> ChaosTimeline:
    """A timeline for a run that injected, was noticed, and recovered."""
    fields: dict[str, float | None] = {
        "scenario_started_at": 100.0,
        "trigger_fired_at": 110.0,
        "injected_at": 112.0,
        "first_action_at": 120.0,
        "recovery_observed_at": 145.0,
    }
    fields.update(overrides)
    return ChaosTimeline(**fields)


def test_durations_anchor_on_injection_not_on_the_scenario_start() -> None:
    """Trigger latency is task design, never charged to the agent's detection."""
    metrics = derive_timing_metrics(
        _observed_timeline(),
        injected=True,
        detection_configured=True,
        detection_fired=True,
        recovery_status="pass",
        recovery_mode="converge",
    )

    # 120 - 112, not 120 - 100: the 10s the trigger spent waiting is not
    # detection time.
    assert metrics["ttd_seconds"] == 8.0
    assert metrics["ttd_status"] == "observed"
    assert metrics["ttr_seconds"] == 33.0
    assert metrics["ttr_status"] == "recovered"
    # The trigger's own latency is still reported, just separately.
    assert metrics["trigger_latency_seconds"] == 10.0


def test_observed_detection_records_its_provenance() -> None:
    """A TTD number always says how it was measured; an absent one claims nothing."""
    observed = derive_timing_metrics(
        _observed_timeline(),
        injected=True,
        detection_configured=True,
        detection_fired=True,
    )
    assert observed["ttd_source"] == "cluster_action"

    unobserved = derive_timing_metrics(
        _observed_timeline(first_action_at=None),
        injected=True,
        detection_configured=True,
        detection_fired=False,
    )
    assert unobserved["ttd_source"] is None


def test_watcher_that_saw_nothing_is_censored_rather_than_zero() -> None:
    """A closed window is "we stopped watching", not "the agent never acted at 0s"."""
    metrics = derive_timing_metrics(
        _observed_timeline(first_action_at=None),
        injected=True,
        detection_configured=True,
        detection_fired=False,
    )

    assert metrics["ttd_status"] == "not_observed"
    assert metrics["ttd_seconds"] is None


def test_no_watcher_is_unavailable_not_unobserved() -> None:
    """A fault with no blast radius to watch never accuses the agent of inaction."""
    metrics = derive_timing_metrics(
        _observed_timeline(first_action_at=None),
        injected=True,
        detection_configured=False,
        detection_fired=False,
    )

    assert metrics["ttd_status"] == "unavailable"
    assert metrics["ttd_seconds"] is None


def test_trigger_that_never_fired_reports_skipped_on_both_axes() -> None:
    """Nothing broke, so there is nothing to detect and nothing to recover."""
    metrics = derive_timing_metrics(
        ChaosTimeline(scenario_started_at=100.0),
        injected=False,
        detection_configured=False,
        detection_fired=False,
    )

    assert metrics["ttd_status"] == "skipped"
    assert metrics["ttr_status"] == "skipped"
    assert metrics["ttd_seconds"] is None
    assert metrics["ttr_seconds"] is None
    assert metrics["trigger_latency_seconds"] is None


def test_failed_injection_is_unavailable_not_skipped() -> None:
    """The trigger fired but the fault did not land — a distinct, reportable outcome."""
    metrics = derive_timing_metrics(
        ChaosTimeline(scenario_started_at=100.0, trigger_fired_at=110.0),
        injected=False,
        detection_configured=False,
        detection_fired=False,
        recovery_status="fail",
        recovery_mode="converge",
    )

    assert metrics["ttd_status"] == "unavailable"
    assert metrics["ttr_status"] == "unavailable"


def test_failing_converge_check_is_censored_and_failing_assert_is_definite() -> None:
    """Mode is what separates "we stopped looking" from "we looked and it was broken"."""
    timeline = _observed_timeline(recovery_observed_at=None)
    censored = derive_timing_metrics(
        timeline,
        injected=True,
        detection_configured=True,
        detection_fired=True,
        recovery_status="fail",
        recovery_mode="converge",
    )
    definite = derive_timing_metrics(
        timeline,
        injected=True,
        detection_configured=True,
        detection_fired=True,
        recovery_status="fail",
        recovery_mode="assert",
    )

    assert censored["ttr_status"] == "censored"
    assert definite["ttr_status"] == "not_recovered"
    # Neither invents a number out of the window length.
    assert censored["ttr_seconds"] is None
    assert definite["ttr_seconds"] is None


def test_errored_recovery_check_is_unmeasured_not_unrecovered() -> None:
    """A check that could not run says nothing about the workload's state."""
    metrics = derive_timing_metrics(
        _observed_timeline(recovery_observed_at=None),
        injected=True,
        detection_configured=True,
        detection_fired=True,
        recovery_status="error",
        recovery_mode="converge",
    )

    assert metrics["ttr_status"] == "unavailable"


def test_fired_watcher_without_an_injection_anchor_reports_no_number() -> None:
    """A fault that never stamped ``injected_at`` leaves the span unanchored."""
    metrics = derive_timing_metrics(
        ChaosTimeline(trigger_fired_at=110.0, first_action_at=120.0),
        injected=True,
        detection_configured=True,
        detection_fired=True,
        recovery_status="pass",
        recovery_mode="converge",
    )

    assert metrics["ttd_status"] == "unavailable"
    assert metrics["ttd_seconds"] is None
    assert metrics["ttr_status"] == "unavailable"


def test_watcher_racing_the_injection_reads_as_immediate() -> None:
    """A baseline poll a few ms ahead of injection means "at once", not "impossible"."""
    metrics = derive_timing_metrics(
        ChaosTimeline(injected_at=112.0, trigger_fired_at=110.0, first_action_at=111.9),
        injected=True,
        detection_configured=True,
        detection_fired=True,
    )

    assert metrics["ttd_seconds"] == 0.0
    assert metrics["ttd_status"] == "observed"


def test_self_reverting_fault_reports_how_long_it_was_disrupted() -> None:
    metrics = derive_timing_metrics(
        _observed_timeline(reverted_at=142.0),
        injected=True,
        detection_configured=True,
        detection_fired=True,
    )

    assert metrics["injection_duration_seconds"] == 30.0


def test_every_emitted_status_is_in_the_declared_vocabulary() -> None:
    """The status strings are a contract with the aggregation layer."""
    cases = [
        dict(injected=False, detection_configured=False, detection_fired=False),
        dict(injected=True, detection_configured=False, detection_fired=False),
        dict(injected=True, detection_configured=True, detection_fired=False),
        dict(injected=True, detection_configured=True, detection_fired=True),
    ]
    for recovery_status in (None, "pass", "fail", "error"):
        for recovery_mode in (None, "converge", "assert"):
            for case in cases:
                metrics = derive_timing_metrics(
                    _observed_timeline(),
                    recovery_status=recovery_status,  # type: ignore[arg-type]
                    recovery_mode=recovery_mode,
                    **case,
                )
                assert metrics["ttd_status"] in TTD_STATUSES
                assert metrics["ttr_status"] in TTR_STATUSES


def test_timeline_serializes_every_anchor_even_when_unset() -> None:
    """A partial timeline still shows how far the run got, key by key."""
    dumped = ChaosTimeline(injected_at=1.0).to_dict()

    assert dumped["injected_at"] == 1.0
    assert dumped["recovery_observed_at"] is None
    assert set(dumped) == {
        "scenario_started_at",
        "trigger_fired_at",
        "injected_at",
        "reverted_at",
        "first_action_at",
        "recovery_observed_at",
        "agent_started_at",
        "agent_finished_at",
    }
