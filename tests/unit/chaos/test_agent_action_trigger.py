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

"""Tests for the ``agent_action`` chaos trigger.

Every test drives the real ``wait`` loop with a mocked ``get_resource`` and
the poll delays patched down, so the observation logic (equals / change /
resourceVersion modes, list watches, stop and timeout handling) is exercised
without a cluster and without real sleeping.
"""

from __future__ import annotations

import threading
from collections.abc import Generator
from typing import Any
from unittest.mock import patch

import pytest

from devops_bench.chaos import ChaosSpec
from devops_bench.chaos.triggers import agent_action
from devops_bench.chaos.triggers.agent_action import AgentActionTrigger
from devops_bench.core.context import RunContext


def _ctx() -> RunContext:
    return RunContext(task_id="t1")


def _policy(name: str, action: str, version: str = "1") -> dict[str, Any]:
    return {
        "metadata": {"name": name, "resourceVersion": version},
        "spec": {"validationFailureAction": action},
    }


@pytest.fixture(autouse=True)
def _fast_poll() -> Generator[None, None, None]:
    """Shrink the poll pacing so multi-iteration tests finish in milliseconds."""
    with (
        patch.object(agent_action, "_POLL_INITIAL_DELAY_SEC", 0.001),
        patch.object(agent_action, "_POLL_MAX_DELAY_SEC", 0.002),
    ):
        yield


def test_equals_mode_fires_when_the_value_appears() -> None:
    """``path`` + ``equals``: fires once the watched value matches."""
    trigger = AgentActionTrigger(
        kind="clusterpolicy",
        resource_name="disallow-privileged-containers",
        path="spec.validationFailureAction",
        equals="Enforce",
        timeout_seconds=30,
    )
    reads = [
        _policy("disallow-privileged-containers", "Audit"),
        _policy("disallow-privileged-containers", "Audit"),
        _policy("disallow-privileged-containers", "Enforce"),
    ]
    with patch.object(agent_action, "get_resource", side_effect=reads) as get_mock:
        assert trigger.wait(_ctx()) is True
    # Named watch: every read asked for the one object.
    assert get_mock.call_args.args == ("clusterpolicy", "disallow-privileged-containers")


def test_equals_mode_fires_immediately_when_already_true() -> None:
    """The equals mode is stateless: a precondition that already holds fires."""
    trigger = AgentActionTrigger(
        kind="clusterpolicy",
        resource_name="p",
        path="spec.validationFailureAction",
        equals="Enforce",
        timeout_seconds=30,
    )
    with patch.object(
        agent_action, "get_resource", return_value=_policy("p", "Enforce")
    ) as get_mock:
        assert trigger.wait(_ctx()) is True
    # One condition read; no baseline snapshot is taken in equals mode.
    get_mock.assert_called_once()


def test_change_mode_fires_when_the_path_value_changes_from_baseline() -> None:
    """``path`` without ``equals``: fires on any change from the wait-start value."""
    trigger = AgentActionTrigger(
        kind="clusterpolicy",
        resource_name="p",
        path="spec.validationFailureAction",
        timeout_seconds=30,
    )
    reads = [
        _policy("p", "Audit"),  # baseline snapshot
        _policy("p", "Audit"),  # unchanged
        _policy("p", "Enforce"),  # changed -> fire
    ]
    with patch.object(agent_action, "get_resource", side_effect=reads):
        assert trigger.wait(_ctx()) is True


def test_no_path_fires_on_any_resource_version_change() -> None:
    """Without ``path``, any mutation (resourceVersion bump) fires."""
    trigger = AgentActionTrigger(
        kind="deployment",
        resource_name="web",
        namespace="team-alpha",
        timeout_seconds=30,
    )
    reads = [
        _policy("web", "x", version="100"),  # baseline
        _policy("web", "x", version="100"),  # untouched
        _policy("web", "x", version="101"),  # mutated -> fire
    ]
    with patch.object(agent_action, "get_resource", side_effect=reads) as get_mock:
        assert trigger.wait(_ctx()) is True
    assert get_mock.call_args.kwargs["namespace"] == "team-alpha"


def test_list_watch_fires_when_any_object_matches() -> None:
    """No ``resource_name``: every object of the kind is watched; ANY match fires."""
    trigger = AgentActionTrigger(
        kind="clusterpolicy",
        path="spec.validationFailureAction",
        equals="Enforce",
        timeout_seconds=30,
    )
    reads = [
        {"items": [_policy("a", "Audit"), _policy("b", "Audit")]},
        {"items": [_policy("a", "Audit"), _policy("b", "Enforce")]},
    ]
    with patch.object(agent_action, "get_resource", side_effect=reads):
        assert trigger.wait(_ctx()) is True


def test_change_mode_fires_when_a_new_object_of_the_kind_appears() -> None:
    """An object missing from the baseline that later exists is a mutation."""
    trigger = AgentActionTrigger(kind="clusterpolicy", timeout_seconds=30)
    reads = [
        {"items": [_policy("a", "Audit")]},  # baseline: only 'a'
        {"items": [_policy("a", "Audit"), _policy("b", "Audit")]},  # 'b' created
    ]
    with patch.object(agent_action, "get_resource", side_effect=reads):
        assert trigger.wait(_ctx()) is True


def test_timeout_returns_false() -> None:
    """The condition never occurring skips the fault (returns False)."""
    trigger = AgentActionTrigger(
        kind="clusterpolicy",
        resource_name="p",
        path="spec.validationFailureAction",
        equals="Enforce",
        timeout_seconds=1,  # schema minimum
    )
    with (
        patch.object(agent_action, "get_resource", return_value=_policy("p", "Audit")),
        # Short-circuit the bounded poll: the pacing is already covered above;
        # this pins only the timeout -> False translation.
        patch.object(agent_action, "poll_until", return_value=False),
    ):
        assert trigger.wait(_ctx()) is False


def test_stop_event_set_returns_false_promptly() -> None:
    """A stop that arrives before the condition fires skips the fault."""
    trigger = AgentActionTrigger(
        kind="clusterpolicy",
        resource_name="p",
        path="spec.validationFailureAction",
        equals="Enforce",
        timeout_seconds=1800,
    )
    stop = threading.Event()
    stop.set()
    with patch.object(agent_action, "get_resource", return_value=_policy("p", "Audit")):
        assert trigger.wait(_ctx(), stop=stop) is False


def test_transient_read_failures_do_not_crash_the_poll() -> None:
    """A missing resource / API blip reads as "condition not met", not a crash."""
    trigger = AgentActionTrigger(
        kind="clusterpolicy",
        resource_name="p",
        path="spec.validationFailureAction",
        equals="Enforce",
        timeout_seconds=30,
    )
    reads = [
        RuntimeError("the server could not find the requested resource"),
        _policy("p", "Enforce"),
    ]
    with patch.object(agent_action, "get_resource", side_effect=reads):
        assert trigger.wait(_ctx()) is True


def test_requires_agent_running_is_true() -> None:
    """The harness must not gate the agent's start on this trigger firing."""
    assert AgentActionTrigger.requires_agent_running is True


def test_parses_through_chaos_spec() -> None:
    """The registered trigger resolves from an authored chaos entry."""
    spec = ChaosSpec.model_validate(
        {
            "trigger": {
                "type": "agent_action",
                "kind": "clusterpolicy",
                "path": "spec.validationFailureAction",
                "equals": "Enforce",
                "timeout_seconds": 600,
            },
            "action": {
                "type": "generate_load",
                "target": {"service_url": "http://x", "qps": 1},
            },
        }
    )
    assert isinstance(spec.trigger, AgentActionTrigger)
    assert spec.trigger.kind == "clusterpolicy"
    assert spec.trigger.equals == "Enforce"
    assert spec.trigger.resource_name is None
