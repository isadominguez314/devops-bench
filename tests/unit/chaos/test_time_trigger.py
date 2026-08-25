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

"""Tests for the time-delay chaos trigger."""

from __future__ import annotations

import threading
from unittest.mock import patch

from devops_bench.chaos.triggers import time_delay
from devops_bench.chaos.triggers.time_delay import TimeTrigger
from devops_bench.core.context import RunContext


def _ctx() -> RunContext:
    return RunContext(task_id="t1")


def test_zero_delay_fires_without_sleeping() -> None:
    trigger = TimeTrigger(delay_seconds=0)
    with patch.object(time_delay.time, "sleep") as sleep_mock:
        assert trigger.wait(_ctx()) is True
    sleep_mock.assert_not_called()


def test_positive_delay_sleeps_for_that_many_seconds() -> None:
    trigger = TimeTrigger(delay_seconds=4)
    with patch.object(time_delay.time, "sleep") as sleep_mock:
        assert trigger.wait(_ctx()) is True
    sleep_mock.assert_called_once_with(4)


def test_delay_waits_on_the_stop_event_when_one_is_given() -> None:
    """With a stop event, the sleep is ``stop.wait`` so it can end early."""
    trigger = TimeTrigger(delay_seconds=4)
    stop = threading.Event()
    with patch.object(stop, "wait", return_value=False) as wait_mock:
        assert trigger.wait(_ctx(), stop=stop) is True
    wait_mock.assert_called_once_with(4)


def test_stop_set_mid_delay_skips_the_fault() -> None:
    """A stop arriving before the delay elapses returns False (skip).

    A fault injected after the agent has already exited measures nothing;
    the trigger reports "did not fire" instead of firing late.
    """
    trigger = TimeTrigger(delay_seconds=60)
    stop = threading.Event()
    stop.set()  # already stopped: wait(60) returns True immediately
    assert trigger.wait(_ctx(), stop=stop) is False


def test_requires_agent_running_defaults_false() -> None:
    """A time trigger fires independently of the agent, so the harness's
    pre-agent chaos-active gate stays on for it."""
    assert TimeTrigger.requires_agent_running is False
