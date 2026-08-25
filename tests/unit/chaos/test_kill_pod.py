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

"""Tests for the mechanical ``kill_pod`` chaos fault.

``get_resource`` / ``delete_resource`` are mocked at the fault module's seam,
so the tests exercise selector resolution, the fail-closed empty-match path,
the chaos-active signal, and the never-raises contract without a cluster.
"""

from __future__ import annotations

import threading
from typing import Any
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from devops_bench.chaos import ChaosSpec
from devops_bench.chaos.faults import kill_pod as kill_pod_module
from devops_bench.chaos.faults.kill_pod import KillPodFault, PodTarget
from devops_bench.core.context import RunContext


def _ctx() -> RunContext:
    return RunContext(task_id="t1")


def _deployment(match_labels: dict[str, str] | None) -> dict[str, Any]:
    return {"spec": {"selector": {"matchLabels": match_labels or {}}}}


def _pods(*names: str) -> dict[str, Any]:
    return {"items": [{"metadata": {"name": name}} for name in names]}


def test_deployment_target_resolves_selector_and_deletes_by_name() -> None:
    fault = KillPodFault(target=PodTarget(deployment="web", namespace="team-alpha"))
    event = threading.Event()
    reads = [_deployment({"app": "web"}), _pods("web-abc", "web-def")]

    with (
        patch.object(kill_pod_module, "get_resource", side_effect=reads) as get_mock,
        patch.object(kill_pod_module, "delete_resource") as delete_mock,
    ):
        result = fault.inject(_ctx(), event)

    assert result.success is True
    assert result.injected_fault == "kill_pod"
    assert "web-abc" in result.output and "web-def" in result.output
    assert event.is_set()

    # The pod query used the selector resolved from the Deployment's own labels.
    pods_call = get_mock.call_args_list[1]
    assert pods_call.args == ("pods",)
    assert pods_call.kwargs["selector"] == "app=web"
    assert pods_call.kwargs["namespace"] == "team-alpha"

    # Deletion is by explicit name so the record shows exactly what was killed.
    delete_mock.assert_called_once()
    assert delete_mock.call_args.args == ("pod", ["web-abc", "web-def"])
    assert delete_mock.call_args.kwargs["namespace"] == "team-alpha"


def test_explicit_selector_skips_the_deployment_lookup() -> None:
    fault = KillPodFault(target=PodTarget(selector="app=web,tier=front", namespace="ns"))

    with (
        patch.object(kill_pod_module, "get_resource", side_effect=[_pods("web-abc")]) as get_mock,
        patch.object(kill_pod_module, "delete_resource"),
    ):
        result = fault.inject(_ctx())

    assert result.success is True
    # Exactly one read — the pods list; no Deployment fetch happened.
    get_mock.assert_called_once()
    assert get_mock.call_args.kwargs["selector"] == "app=web,tier=front"


def test_no_matching_pods_fails_closed() -> None:
    """A disruption that touched nothing must not read as one that was survived."""
    fault = KillPodFault(target=PodTarget(selector="app=ghost", namespace="ns"))
    event = threading.Event()

    with (
        patch.object(kill_pod_module, "get_resource", return_value=_pods()),
        patch.object(kill_pod_module, "delete_resource") as delete_mock,
    ):
        result = fault.inject(_ctx(), event)

    assert result.success is False
    assert "app=ghost" in (result.error or "")
    delete_mock.assert_not_called()
    assert not event.is_set()


def test_deployment_without_match_labels_fails_closed() -> None:
    fault = KillPodFault(target=PodTarget(deployment="web", namespace="ns"))

    with patch.object(kill_pod_module, "get_resource", return_value=_deployment(None)):
        result = fault.inject(_ctx())

    assert result.success is False
    assert "matchLabels" in (result.error or "")


def test_exceptions_convert_to_failure_instead_of_raising() -> None:
    """One fault must never abort the run — kubectl failures become results."""
    fault = KillPodFault(target=PodTarget(deployment="web", namespace="ns"))

    with patch.object(
        kill_pod_module, "get_resource", side_effect=RuntimeError("connection refused")
    ):
        result = fault.inject(_ctx())

    assert result.success is False
    assert "connection refused" in (result.error or "")


def test_pod_target_requires_exactly_one_selection() -> None:
    with pytest.raises(ValidationError):
        PodTarget(namespace="ns")  # neither
    with pytest.raises(ValidationError):
        PodTarget(deployment="web", selector="app=web", namespace="ns")  # both


def test_parses_through_chaos_spec() -> None:
    """The registered fault resolves from an authored chaos entry."""
    spec = ChaosSpec.model_validate(
        {
            "trigger": {"type": "time", "delay_seconds": 0},
            "action": {
                "type": "kill_pod",
                "target": {"deployment": "web", "namespace": "team-alpha"},
            },
        }
    )
    assert isinstance(spec.action, KillPodFault)
    assert spec.action.target.deployment == "web"
    assert spec.action.target.namespace == "team-alpha"
