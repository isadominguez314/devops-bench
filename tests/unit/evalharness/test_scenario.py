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

"""Scenario wiring tests (CONVENTIONS.md §4.2 / harness handoff §9.1/§9.2).

The scenario manager runs the typed chaos seam (``trigger.wait`` then
``action.inject``) and resolves the chaos ``verify`` key against a
**mapping** (not a list scan) supplied by the orchestrator. Fakes stand in
for both seams so the test exercises only the wiring — never a real cluster
or a real LLM.
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from typing import Any, Literal
from unittest.mock import patch

import pytest

from devops_bench.chaos import ChaosResult, ChaosSpec
from devops_bench.chaos.base import TRIGGERS, Trigger
from devops_bench.chaos.faults.generate_load import GenerateLoadFault
from devops_bench.chaos.triggers.time_delay import TimeTrigger
from devops_bench.core.context import RunContext
from devops_bench.evalharness.scenario import ScenarioManager, pick_free_port
from devops_bench.verification import VerificationResult, VerifierAgent
from devops_bench.verification.base import VERIFIERS, BaseVerifier
from devops_bench.verification.spec import parse_entries


def _build_spec(*, verify_key: str | None) -> ChaosSpec:
    """Build a typed :class:`ChaosSpec` mirroring the optimize-scale entry."""
    return ChaosSpec.model_validate(
        {
            "name": "Test Disruption",
            "trigger": {"type": "time", "delay_seconds": 0},
            "action": {
                "type": "generate_load",
                "target": {
                    "service_url": "http://example.svc.cluster.local",
                    "qps": 50,
                },
            },
            "verify": verify_key,
        }
    )


def _build_ctx() -> RunContext:
    return RunContext(task_id="t", task_name="t")


def test_scenario_drives_trigger_wait_then_action_inject() -> None:
    """The seam: trigger.wait runs first, then action.inject — no inline goal builder."""
    spec = _build_spec(verify_key=None)
    order: list[str] = []

    def fake_wait(self: TimeTrigger, ctx: RunContext, stop: threading.Event | None = None) -> bool:
        order.append("trigger.wait")
        return True

    def fake_inject(
        self: GenerateLoadFault,
        ctx: RunContext,
        event: threading.Event | None,
    ) -> ChaosResult:
        order.append("action.inject")
        if event is not None:
            event.set()
        return ChaosResult(
            success=True,
            injected_fault=self.type,
            output="ok",
            elapsed_time=0.1,
        )

    with (
        patch.object(TimeTrigger, "wait", fake_wait),
        patch.object(GenerateLoadFault, "inject", fake_inject),
    ):
        manager = ScenarioManager(
            target_deployment="dep",
            namespace="ns",
            verification_mapping={},
            skip_port_forward=True,
        )
        manager.run_chaos_and_verification(spec, _build_ctx())

    assert order == ["trigger.wait", "action.inject"]
    chaos_report, perf_report = manager.get_reports()
    assert chaos_report["status"] == "success"
    assert chaos_report["injected_fault"] == "generate_load"
    assert chaos_report["name"] == "Test Disruption"
    # No verification mapping resolution happened (verify_key was None).
    assert "verification" not in chaos_report
    assert perf_report == {}


def test_scenario_threads_port_forward_target_onto_ctx_env() -> None:
    """The manager threads the port-forward target onto ``ctx.env`` for the fault.

    Connectivity moved into the load fault (#33): the manager no longer rewrites
    the action URL or opens a port-forward itself — it hands the fault the
    target deployment / namespace (and the skip flag) via the run context's
    ``env`` so the fault can open its own tunnel.
    """
    from devops_bench.chaos.faults.generate_load import (
        _ENV_SKIP_PORT_FORWARD,
        _ENV_TARGET_DEPLOYMENT,
        _ENV_TARGET_NAMESPACE,
    )

    spec = _build_spec(verify_key=None)
    captured: dict[str, Any] = {}

    def fake_inject(self: GenerateLoadFault, ctx: RunContext, event):
        captured["env"] = dict(ctx.env)
        # The in-cluster URL is untouched by the manager now; the fault is what
        # would point it at the local tunnel (skipped here).
        captured["service_url"] = self.target.service_url
        return ChaosResult(success=True, injected_fault=self.type, elapsed_time=0.0)

    with (
        patch.object(TimeTrigger, "wait", lambda self, ctx, stop=None: True),
        patch.object(GenerateLoadFault, "inject", fake_inject),
    ):
        manager = ScenarioManager(
            target_deployment="dep",
            namespace="ns",
            skip_port_forward=True,
        )
        manager.run_chaos_and_verification(spec, _build_ctx())

    assert captured["env"][_ENV_TARGET_DEPLOYMENT] == "dep"
    assert captured["env"][_ENV_TARGET_NAMESPACE] == "ns"
    # ``skip_port_forward=True`` flips the fault's opt-out flag on the env.
    assert captured["env"][_ENV_SKIP_PORT_FORWARD] == "1"
    # The manager leaves the action's in-cluster URL alone — no rewrite seam.
    assert captured["service_url"] == "http://example.svc.cluster.local"


def test_scenario_threads_custom_local_port_onto_ctx_env() -> None:
    """A per-run ``local_port`` is threaded onto ``ctx.env`` for the fault.

    Connectivity lives in the fault, so the manager hands it the per-run local
    port via ``CHAOS_LOCAL_PORT``; the fault binds the port-forward's local side
    there. No port is threaded when ``local_port`` is None (default behavior).
    """
    from devops_bench.chaos.faults.generate_load import _ENV_LOCAL_PORT

    spec = _build_spec(verify_key=None)
    captured: dict[str, Any] = {}

    def fake_inject(self: GenerateLoadFault, ctx: RunContext, event):
        captured["env"] = dict(ctx.env)
        return ChaosResult(success=True, injected_fault=self.type, elapsed_time=0.0)

    with (
        patch.object(TimeTrigger, "wait", lambda self, ctx, stop=None: True),
        patch.object(GenerateLoadFault, "inject", fake_inject),
    ):
        manager = ScenarioManager(
            target_deployment="dep",
            namespace="ns",
            skip_port_forward=True,
            local_port=34567,
        )
        assert manager.local_port == 34567
        manager.run_chaos_and_verification(spec, _build_ctx())

    assert captured["env"][_ENV_LOCAL_PORT] == "34567"


def test_scenario_omits_local_port_env_by_default() -> None:
    """Without a per-run port, no ``CHAOS_LOCAL_PORT`` is threaded (fault default)."""
    from devops_bench.chaos.faults.generate_load import _ENV_LOCAL_PORT

    spec = _build_spec(verify_key=None)
    captured: dict[str, Any] = {}

    def fake_inject(self: GenerateLoadFault, ctx: RunContext, event):
        captured["env"] = dict(ctx.env)
        return ChaosResult(success=True, injected_fault=self.type, elapsed_time=0.0)

    with (
        patch.object(TimeTrigger, "wait", lambda self, ctx, stop=None: True),
        patch.object(GenerateLoadFault, "inject", fake_inject),
    ):
        ScenarioManager(
            target_deployment="dep", namespace="ns", skip_port_forward=True
        ).run_chaos_and_verification(spec, _build_ctx())

    assert _ENV_LOCAL_PORT not in captured["env"]


def test_pick_free_port_returns_a_usable_port() -> None:
    # pick_free_port binds port 0 and releases it, so consecutive calls may reuse
    # the same port — it guarantees a usable ephemeral port, not distinctness.
    port = pick_free_port()
    assert isinstance(port, int)
    assert 1 <= port <= 65535


def test_scenario_resolves_verify_against_mapping() -> None:
    """The chaos ``verify`` key is looked up in the harness-supplied mapping."""
    spec = _build_spec(verify_key="planned-verify")
    verification_entry = SimpleNamespace(check=object(), resolved_mode="converge")

    fake_result = VerificationResult(success=True, elapsed_time=2.5, reason="all good")

    with (
        patch.object(TimeTrigger, "wait", lambda self, ctx, stop=None: True),
        patch.object(
            GenerateLoadFault,
            "inject",
            lambda self, ctx, event: ChaosResult(
                success=True, injected_fault=self.type, elapsed_time=0.0
            ),
        ),
        patch.object(VerifierAgent, "run_entry", return_value=fake_result) as mock_run_entry,
    ):
        manager = ScenarioManager(
            target_deployment="dep",
            namespace="ns",
            verification_mapping={"planned-verify": verification_entry},
            skip_port_forward=True,
        )
        manager.run_chaos_and_verification(spec, _build_ctx())

    # The mapping value's entry (not a list-scanned dict, and not just its
    # ``check`` node) flowed straight to the VerifierAgent: the lookup is O(1),
    # never imports verification on the chaos side, and the entry's resolved
    # mode (converge vs assert) governs the check.
    mock_run_entry.assert_called_once_with(verification_entry, timeout_sec=120)

    chaos_report, perf_report = manager.get_reports()
    assert chaos_report["verification"]["success"] is True
    assert chaos_report["verification"]["reason"] == "all good"
    # Perf report is derived from the typed VerificationResult.
    assert perf_report == {
        "deployment_time_seconds": 2.5,
        "uptime_percentage": 100.0,
        "resource_utilization_efficiency": 1.0,
    }


@VERIFIERS.register("scenario_counting")
class _ScenarioCounting(BaseVerifier):
    """Test double recording every budget it was called with.

    Mirrors the assert-mode doubles in test_combinators.py / test_run_entry.py,
    but exercised through the real (unmocked) ``VerifierAgent`` so the
    scenario wiring itself — not a mocked ``run_entry`` — is what proves the
    entry's resolved mode governs the poll.
    """

    type: Literal["scenario_counting"]
    budgets: list[float] = []

    def verify(self, timeout_sec: float) -> VerificationResult:
        self.budgets.append(timeout_sec)
        return VerificationResult(success=False, elapsed_time=0.0, reason="stub", name=self.name)


def test_chaos_referenced_assert_mode_entry_evaluates_single_shot() -> None:
    """A chaos ``verify:`` referencing an assert-mode safeguard polls exactly once.

    Before this fix, the manager unwrapped the entry's ``check`` node and
    always converge-polled it via ``wait_for_condition``, which would give an
    already-happened safeguard violation up to ``VERIFICATION_TIMEOUT_SEC``
    (120s) to heal. Routing through ``VerifierAgent.run_entry`` instead makes
    the entry's resolved mode govern: an assert-mode safeguard evaluates once
    with a zero budget, regardless of the timeout passed in.
    """
    entries, errors = parse_entries(
        [
            {
                "name": "planned-verify",
                "role": "safeguard",
                "severity": "catastrophic",
                "check": {"type": "scenario_counting", "budgets": []},
            }
        ]
    )
    assert errors == []
    entry = entries[0]
    assert entry.resolved_mode == "assert"

    spec = _build_spec(verify_key="planned-verify")

    with (
        patch.object(TimeTrigger, "wait", lambda self, ctx, stop=None: True),
        patch.object(
            GenerateLoadFault,
            "inject",
            lambda self, ctx, event: ChaosResult(
                success=True, injected_fault=self.type, elapsed_time=0.0
            ),
        ),
    ):
        manager = ScenarioManager(
            target_deployment="dep",
            namespace="ns",
            verification_mapping={"planned-verify": entry},
            skip_port_forward=True,
        )
        manager.run_chaos_and_verification(spec, _build_ctx())

    # Called exactly once, with a zero budget — single-shot, not a 120s poll.
    assert entry.check.budgets == [0.0]

    chaos_report, _ = manager.get_reports()
    assert chaos_report["verification"]["success"] is False


def test_scenario_unknown_verify_key_surfaces_failure_into_report() -> None:
    """An unmapped ``verify:`` key writes a verification-failure entry, not silence.

    The chaos seam never silently drops a verify reference — a typo'd key
    must be visible on ``results.json``, not just in the log, so the
    operator can spot a broken cross-reference without trawling stdout.
    """
    spec = _build_spec(verify_key="not-in-mapping")

    with (
        patch.object(TimeTrigger, "wait", lambda self, ctx, stop=None: True),
        patch.object(
            GenerateLoadFault,
            "inject",
            lambda self, ctx, event: ChaosResult(
                success=True, injected_fault=self.type, elapsed_time=0.0
            ),
        ),
        patch.object(VerifierAgent, "run_entry") as mock_run_entry,
    ):
        manager = ScenarioManager(
            target_deployment="dep",
            namespace="ns",
            verification_mapping={"some-other-key": object()},
            skip_port_forward=True,
        )
        manager.run_chaos_and_verification(spec, _build_ctx())

    # Never called — the unknown key short-circuited before dispatch.
    mock_run_entry.assert_not_called()
    chaos_report, _ = manager.get_reports()
    assert chaos_report["status"] == "success"
    verification = chaos_report["verification"]
    assert verification["success"] is False
    assert verification["unresolved_reference"] == "not-in-mapping"
    assert verification["known_references"] == ["some-other-key"]
    assert "not found" in verification["reason"]


def test_chaos_failure_lands_typed_error_into_report() -> None:
    """A ``ChaosResult(success=False, error=...)`` flows into the chaos report."""
    spec = _build_spec(verify_key=None)

    def failing_inject(self, ctx, event):
        return ChaosResult(
            success=False,
            injected_fault=self.type,
            elapsed_time=0.0,
            error="fortio not found",
        )

    with (
        patch.object(TimeTrigger, "wait", lambda self, ctx, stop=None: True),
        patch.object(GenerateLoadFault, "inject", failing_inject),
    ):
        manager = ScenarioManager(
            target_deployment="dep",
            namespace="ns",
            skip_port_forward=True,
        )
        manager.run_chaos_and_verification(spec, _build_ctx())

    chaos_report, _ = manager.get_reports()
    assert chaos_report["status"] == "failed"
    assert chaos_report["error"] == "fortio not found"


def test_injection_exception_sets_chaos_active_event() -> None:
    """A raising injection still sets the event so the main thread unblocks.

    The main thread waits on ``chaos_active_event`` to learn the disruption is
    active; if injection raises and the event is never set, it stalls for the
    full activation timeout. The failure path must signal the event.
    """
    spec = _build_spec(verify_key=None)

    def raising_inject(self, ctx, event):
        raise RuntimeError("port-forward refused")

    with (
        patch.object(TimeTrigger, "wait", lambda self, ctx, stop=None: True),
        patch.object(GenerateLoadFault, "inject", raising_inject),
    ):
        manager = ScenarioManager(
            target_deployment="dep",
            namespace="ns",
            skip_port_forward=True,
        )
        manager.run_chaos_and_verification(spec, _build_ctx())

    assert manager.chaos_active_event.is_set()
    chaos_report, _ = manager.get_reports()
    assert chaos_report["status"] == "failed"


def test_stop_aborts_verification() -> None:
    """``stop()`` is exception-safe and sets the abort flag once."""
    manager = ScenarioManager(
        target_deployment="dep",
        namespace="ns",
        skip_port_forward=True,
    )
    manager.stop()  # The fault owns the port-forward; stop only sets the flag.
    assert manager._aborted.is_set()  # noqa: SLF001
    # stop() also releases a trigger still waiting on the agent's action.
    assert manager._trigger_stop.is_set()  # noqa: SLF001
    # Idempotent — the second call must not raise.
    manager.stop()


def test_notify_agent_done_releases_trigger_without_aborting_verification() -> None:
    """``notify_agent_done()`` stops a waiting trigger but not a pending verification.

    The agent finishing means an unfired trigger can never fire — but a fault
    that already injected must still get its verification, so only the
    trigger-stop event is set, never the abort flag.
    """
    manager = ScenarioManager(
        target_deployment="dep",
        namespace="ns",
        skip_port_forward=True,
    )
    manager.notify_agent_done()
    assert manager._trigger_stop.is_set()  # noqa: SLF001
    assert not manager._aborted.is_set()  # noqa: SLF001
    # Idempotent — the second call must not raise.
    manager.notify_agent_done()


def test_trigger_declining_to_fire_records_skipped_and_skips_everything() -> None:
    """A trigger returning False skips injection AND verification, status='skipped'.

    The skipped outcome is distinct from a failed injection: the fault never
    ran, so verifying "recovery" from it would measure nothing. The
    chaos-active event is still set so a waiting main thread unblocks.
    """
    spec = _build_spec(verify_key="planned-verify")

    with (
        patch.object(TimeTrigger, "wait", lambda self, ctx, stop=None: False),
        patch.object(
            GenerateLoadFault,
            "inject",
            side_effect=AssertionError("a skipped trigger must never inject"),
        ),
        patch.object(VerifierAgent, "run_entry") as mock_run_entry,
    ):
        manager = ScenarioManager(
            target_deployment="dep",
            namespace="ns",
            verification_mapping={"planned-verify": object()},
            skip_port_forward=True,
        )
        manager.run_chaos_and_verification(spec, _build_ctx())

    mock_run_entry.assert_not_called()
    assert manager.chaos_active_event.is_set()
    chaos_report, perf_report = manager.get_reports()
    assert chaos_report["status"] == "skipped"
    assert chaos_report["injected_fault"] == "generate_load"
    assert "did not fire" in chaos_report["reason"]
    assert perf_report == {}


def test_legacy_trigger_returning_none_still_fires() -> None:
    """A pre-signature external trigger returning ``None`` injects as before.

    Only an explicit ``False`` skips; ``None`` (the old contract's return
    value) must keep meaning "fired" so entry-point triggers that predate the
    bool contract are not silently downgraded to never firing.
    """
    spec = _build_spec(verify_key=None)
    injected: list[str] = []

    def legacy_wait(self: TimeTrigger, ctx: RunContext, stop=None) -> None:
        return None

    with (
        patch.object(TimeTrigger, "wait", legacy_wait),
        patch.object(
            GenerateLoadFault,
            "inject",
            lambda self, ctx, event: (
                injected.append(self.type),
                ChaosResult(success=True, injected_fault=self.type, elapsed_time=0.0),
            )[1],
        ),
    ):
        manager = ScenarioManager(
            target_deployment="dep",
            namespace="ns",
            skip_port_forward=True,
        )
        manager.run_chaos_and_verification(spec, _build_ctx())

    assert injected == ["generate_load"]
    chaos_report, _ = manager.get_reports()
    assert chaos_report["status"] == "success"


def test_scenario_never_resolves_lb_for_action_without_load_url() -> None:
    """An action with no ``target.service_url`` (kill_pod) skips LB resolution.

    The LB lookup exists purely to route load at the workload; for a fault
    that generates no load it would waste up to the LB timeout resolving an
    IP nothing consumes.
    """
    from devops_bench.chaos.faults.kill_pod import KillPodFault

    spec = ChaosSpec.model_validate(
        {
            "name": "Pod Kill",
            "trigger": {"type": "time", "delay_seconds": 0},
            "action": {
                "type": "kill_pod",
                "target": {"deployment": "web", "namespace": "team-alpha"},
            },
        }
    )
    captured: dict[str, Any] = {}

    def fake_inject(self: KillPodFault, ctx: RunContext, event):
        captured["env"] = dict(ctx.env)
        return ChaosResult(success=True, injected_fault=self.type, elapsed_time=0.0)

    with (
        patch.object(TimeTrigger, "wait", lambda self, ctx, stop=None: True),
        patch.object(KillPodFault, "inject", fake_inject),
        # kill_pod's real detection watcher polls the target Deployment; this
        # test is about LB resolution, and a watcher thread shelling out to
        # kubectl against a cluster that does not exist is not part of it.
        patch.object(KillPodFault, "detection_watch", lambda self: None),
        patch(
            "devops_bench.evalharness.scenario.get_resource",
            side_effect=AssertionError("kill_pod must not trigger LB resolution"),
        ),
        patch(
            "devops_bench.evalharness.scenario.poll_until",
            side_effect=AssertionError("kill_pod must not poll for an LB IP"),
        ),
    ):
        manager = ScenarioManager(
            target_deployment="dep",
            namespace="ns",
            skip_port_forward=False,  # real-cluster path — the gate under test
        )
        manager.run_chaos_and_verification(spec, _build_ctx())

    chaos_report, _ = manager.get_reports()
    assert chaos_report["status"] == "success"
    assert chaos_report["injected_fault"] == "kill_pod"


def test_scenario_resolves_lb_ip_and_points_action_url_at_it() -> None:
    """Real-cluster path: manager resolves the Service LB IP and rewrites the URL.

    The harness no longer relies on a port-forward to reach the workload — it
    reads ``status.loadBalancer.ingress[0].ip`` from the target Service and
    rewrites the action's ``target.service_url`` to ``http://<ip>:8080`` so the
    fortio spike hits the LB directly. The skip flag is set on ``ctx.env`` so
    the fault does not also open a redundant tunnel.
    """
    from devops_bench.chaos.faults.generate_load import (
        _ENV_SKIP_PORT_FORWARD,
        _ENV_TARGET_DEPLOYMENT,
        _ENV_TARGET_NAMESPACE,
    )

    spec = _build_spec(verify_key=None)
    captured: dict[str, Any] = {}

    def fake_inject(self: GenerateLoadFault, ctx: RunContext, event):
        captured["env"] = dict(ctx.env)
        captured["service_url"] = self.target.service_url
        return ChaosResult(success=True, injected_fault=self.type, elapsed_time=0.0)

    fake_svc = {"status": {"loadBalancer": {"ingress": [{"ip": "34.10.20.30"}]}}}

    with (
        patch.object(TimeTrigger, "wait", lambda self, ctx, stop=None: True),
        patch.object(GenerateLoadFault, "inject", fake_inject),
        patch(
            "devops_bench.evalharness.scenario.get_resource",
            return_value=fake_svc,
        ) as get_resource_mock,
    ):
        manager = ScenarioManager(
            target_deployment="dep",
            namespace="ns",
            skip_port_forward=False,
        )
        manager.run_chaos_and_verification(spec, _build_ctx())

    # The Service was queried by deployment name in the right namespace.
    get_resource_mock.assert_called_with("service", "dep", namespace="ns")
    # The action's URL was rewritten to the LB endpoint, port 8080.
    assert captured["service_url"] == "http://34.10.20.30:8080"
    # Skip flag is set so the fault doesn't also open a port-forward.
    assert captured["env"][_ENV_SKIP_PORT_FORWARD] == "1"
    # Deployment / namespace are still threaded for the fallback path.
    assert captured["env"][_ENV_TARGET_DEPLOYMENT] == "dep"
    assert captured["env"][_ENV_TARGET_NAMESPACE] == "ns"


def test_scenario_falls_back_to_port_forward_when_lb_ip_unavailable() -> None:
    """When LB resolution times out, the manager leaves the skip flag unset.

    The fault then opens its own ``kubectl port-forward`` to the target
    deployment as the fallback transport — a degraded but functional path so
    the run still attempts load.
    """
    from devops_bench.chaos.faults.generate_load import _ENV_SKIP_PORT_FORWARD

    spec = _build_spec(verify_key=None)
    captured: dict[str, Any] = {}

    def fake_inject(self: GenerateLoadFault, ctx: RunContext, event):
        captured["env"] = dict(ctx.env)
        captured["service_url"] = self.target.service_url
        return ChaosResult(success=True, injected_fault=self.type, elapsed_time=0.0)

    # Service has no LB ingress assigned yet — every poll returns "not ready",
    # and the bounded poll eventually gives up.
    no_ip_svc = {"status": {"loadBalancer": {}}}

    with (
        patch.object(TimeTrigger, "wait", lambda self, ctx, stop=None: True),
        patch.object(GenerateLoadFault, "inject", fake_inject),
        patch(
            "devops_bench.evalharness.scenario.get_resource",
            return_value=no_ip_svc,
        ),
        # Make poll_until short-circuit to a single failed check so the test
        # doesn't actually wait 180s on the wall clock.
        patch(
            "devops_bench.evalharness.scenario.poll_until",
            return_value=False,
        ) as poll_mock,
    ):
        manager = ScenarioManager(
            target_deployment="dep",
            namespace="ns",
            skip_port_forward=False,
        )
        manager.run_chaos_and_verification(spec, _build_ctx())

    poll_mock.assert_called_once()
    # The action URL is untouched (no IP to rewrite to).
    assert captured["service_url"] == "http://example.svc.cluster.local"
    # And critically the skip flag is NOT set — the fault falls back to its
    # port-forward to keep the run functional.
    assert _ENV_SKIP_PORT_FORWARD not in captured["env"]


def test_scenario_skips_lb_resolution_in_smoke_path() -> None:
    """``skip_port_forward=True`` (smoke) never queries the cluster for an LB IP.

    The smoke harness runs against NoOpDeployer with no real cluster, so
    asking kubectl for a Service would either fail or accidentally hit the
    operator's current context. The skip flag short-circuits both the
    resolution and the port-forward.
    """
    from devops_bench.chaos.faults.generate_load import _ENV_SKIP_PORT_FORWARD

    spec = _build_spec(verify_key=None)
    captured: dict[str, Any] = {}

    def fake_inject(self: GenerateLoadFault, ctx: RunContext, event):
        captured["env"] = dict(ctx.env)
        captured["service_url"] = self.target.service_url
        return ChaosResult(success=True, injected_fault=self.type, elapsed_time=0.0)

    with (
        patch.object(TimeTrigger, "wait", lambda self, ctx, stop=None: True),
        patch.object(GenerateLoadFault, "inject", fake_inject),
        patch(
            "devops_bench.evalharness.scenario.get_resource",
            side_effect=AssertionError("smoke path must not query the cluster"),
        ),
        patch(
            "devops_bench.evalharness.scenario.poll_until",
            side_effect=AssertionError("smoke path must not poll"),
        ),
    ):
        manager = ScenarioManager(
            target_deployment="dep",
            namespace="ns",
            skip_port_forward=True,
        )
        manager.run_chaos_and_verification(spec, _build_ctx())

    # Skip flag set, URL untouched, no kubectl / poll happened (the patches
    # would have raised AssertionError otherwise).
    assert captured["env"][_ENV_SKIP_PORT_FORWARD] == "1"
    assert captured["service_url"] == "http://example.svc.cluster.local"


def test_scenario_skips_lb_resolution_for_local_cluster() -> None:
    """When the cluster is local (location='local'), skip LB resolution but keep port-forward fallback.

    This avoids the 180s LoadBalancer IP resolution timeout on KinD, where
    the Service type is ClusterIP and will never get an external IP, while
    still allowing the port-forward tunnel to be opened.
    """
    from devops_bench.chaos.faults.generate_load import (
        _ENV_SKIP_PORT_FORWARD,
        _ENV_TARGET_DEPLOYMENT,
        _ENV_TARGET_NAMESPACE,
    )
    from devops_bench.core.context import ClusterInfo

    spec = _build_spec(verify_key=None)
    captured: dict[str, Any] = {}

    def fake_inject(self: GenerateLoadFault, ctx: RunContext, event):
        captured["env"] = dict(ctx.env)
        captured["service_url"] = self.target.service_url
        return ChaosResult(success=True, injected_fault=self.type, elapsed_time=0.0)

    # Local cluster info
    local_cluster = ClusterInfo(name="my-kind", location="local")
    ctx = _build_ctx()
    ctx.cluster = local_cluster

    with (
        patch.object(TimeTrigger, "wait", lambda self, ctx, stop=None: True),
        patch.object(GenerateLoadFault, "inject", fake_inject),
        patch(
            "devops_bench.evalharness.scenario.get_resource",
            side_effect=AssertionError("local path must not query the cluster for LB"),
        ),
        patch(
            "devops_bench.evalharness.scenario.poll_until",
            side_effect=AssertionError("local path must not poll for LB"),
        ),
    ):
        manager = ScenarioManager(
            target_deployment="dep",
            namespace="ns",
            skip_port_forward=False,
        )
        manager.run_chaos_and_verification(spec, ctx)

    assert captured["service_url"] == "http://example.svc.cluster.local"
    assert _ENV_SKIP_PORT_FORWARD not in captured["env"]
    assert captured["env"][_ENV_TARGET_DEPLOYMENT] == "dep"
    assert captured["env"][_ENV_TARGET_NAMESPACE] == "ns"


# --- detection watcher / timing metrics ---------------------------------------


@TRIGGERS.register("scenario_detect")
class _ScenarioDetect(Trigger):
    """A watcher double: fires, declines, or raises, without touching a cluster.

    Registered so the spec's ``detect:`` node parses through the real
    :data:`TRIGGERS` registry — the point of running the watcher as a trigger
    is that any registered trigger can serve as one, and a hand-built stub
    would not prove that.
    """

    type: Literal["scenario_detect"]
    fires: bool = True
    raises: bool = False
    waits: list[str] = []

    def wait(self, ctx: RunContext, stop: threading.Event | None = None) -> bool:
        self.waits.append("waited")
        if self.raises:
            raise RuntimeError("watcher exploded")
        if stop is not None and stop.is_set():
            return False
        return self.fires


def _build_watched_spec(**detect: Any) -> ChaosSpec:
    """A load-fault spec whose detection watcher is the double above."""
    return ChaosSpec.model_validate(
        {
            "name": "Watched Disruption",
            "trigger": {"type": "time", "delay_seconds": 0},
            "action": {
                "type": "generate_load",
                "target": {"service_url": "http://example.svc", "qps": 50},
            },
            "detect": {"type": "scenario_detect", "waits": [], **detect},
        }
    )


def _inject_at(injected_at: float):
    """A fake ``inject`` that reports the disruption became real at a fixed instant."""

    def _fake(self: GenerateLoadFault, ctx: RunContext, event) -> ChaosResult:
        if event is not None:
            event.set()
        return ChaosResult(
            success=True,
            injected_fault=self.type,
            elapsed_time=0.0,
            injected_at=injected_at,
        )

    return _fake


def _run_watched(spec: ChaosSpec) -> dict[str, Any]:
    manager = ScenarioManager(target_deployment="dep", namespace="ns", skip_port_forward=True)
    # The detection window is the agent's lifetime, so an agent has to exist
    # for the scenario to hold it open at all.
    manager.mark_agent_started()
    with (
        patch.object(TimeTrigger, "wait", lambda self, ctx, stop=None: True),
        patch.object(GenerateLoadFault, "inject", _inject_at(time.time())),
    ):
        manager.run_chaos_and_verification(spec, _build_ctx())
    chaos_report, _ = manager.get_reports()
    return chaos_report


def test_detection_watcher_fires_and_anchors_ttd_on_the_injection() -> None:
    """The first mutation of the blast radius is the time-to-detect observation."""
    spec = _build_watched_spec(fires=True)

    chaos_report = _run_watched(spec)

    metrics = chaos_report["metrics"]
    assert metrics["ttd_status"] == "observed"
    assert metrics["ttd_source"] == "cluster_action"
    assert metrics["ttd_seconds"] is not None and metrics["ttd_seconds"] >= 0.0
    # The anchor pair the number was derived from is on the record too, so the
    # arithmetic is auditable rather than taken on trust.
    assert chaos_report["timeline"]["injected_at"] is not None
    assert chaos_report["timeline"]["first_action_at"] is not None


def test_watcher_that_declines_records_not_observed_rather_than_a_number() -> None:
    chaos_report = _run_watched(_build_watched_spec(fires=False))

    assert chaos_report["metrics"]["ttd_status"] == "not_observed"
    assert chaos_report["metrics"]["ttd_seconds"] is None
    assert chaos_report["timeline"]["first_action_at"] is None


def test_an_agent_that_has_already_exited_closes_the_detection_window() -> None:
    """A departed agent cannot take a corrective action; the watcher stops polling."""
    spec = _build_watched_spec(fires=True)

    manager = ScenarioManager(target_deployment="dep", namespace="ns", skip_port_forward=True)
    manager.mark_agent_started()
    manager.notify_agent_done()
    with (
        patch.object(TimeTrigger, "wait", lambda self, ctx, stop=None: True),
        patch.object(GenerateLoadFault, "inject", _inject_at(time.time())),
    ):
        manager.run_chaos_and_verification(spec, _build_ctx())

    chaos_report, _ = manager.get_reports()
    # The watcher ran and honored the stop event rather than reporting a fire.
    assert spec.detect.waits == ["waited"]
    assert chaos_report["metrics"]["ttd_status"] == "not_observed"


def test_a_crashing_watcher_costs_the_metric_not_the_run() -> None:
    """A broken detection proxy must never turn a good chaos run into a failed one."""
    chaos_report = _run_watched(_build_watched_spec(raises=True))

    assert chaos_report["status"] == "success"
    assert chaos_report["metrics"]["ttd_status"] == "not_observed"


def test_no_watcher_configured_reports_unavailable_not_unobserved() -> None:
    """A fault with no declared blast radius never accuses the agent of inaction."""
    spec = _build_spec(verify_key=None)
    assert spec.detection_trigger() is None

    manager = ScenarioManager(target_deployment="dep", namespace="ns", skip_port_forward=True)
    with (
        patch.object(TimeTrigger, "wait", lambda self, ctx, stop=None: True),
        patch.object(GenerateLoadFault, "inject", _inject_at(time.time())),
    ):
        manager.run_chaos_and_verification(spec, _build_ctx())

    chaos_report, _ = manager.get_reports()
    assert chaos_report["metrics"]["ttd_status"] == "unavailable"


def test_watcher_is_armed_only_after_the_fault_actually_landed() -> None:
    """A watcher armed before the disruption would time the agent's prior work."""
    spec = _build_watched_spec(fires=True)
    watcher = spec.detect

    manager = ScenarioManager(target_deployment="dep", namespace="ns", skip_port_forward=True)
    manager.mark_agent_started()
    with (
        patch.object(TimeTrigger, "wait", lambda self, ctx, stop=None: True),
        patch.object(
            GenerateLoadFault,
            "inject",
            lambda self, ctx, event: ChaosResult(
                success=False, injected_fault=self.type, elapsed_time=0.0, error="no route"
            ),
        ),
    ):
        manager.run_chaos_and_verification(spec, _build_ctx())

    assert watcher.waits == []
    chaos_report, _ = manager.get_reports()
    # A fault that never disrupted anything has no detection time to report —
    # and reporting one would credit the agent for noticing a non-event.
    assert chaos_report["status"] == "failed"
    assert chaos_report["metrics"]["ttd_status"] == "unavailable"
    assert chaos_report["metrics"]["ttr_status"] == "unavailable"


def test_a_trigger_that_never_fired_reports_skipped_timings() -> None:
    """No fault, so both metrics are "skipped" — distinct from "unavailable"."""
    spec = _build_watched_spec(fires=True)

    manager = ScenarioManager(target_deployment="dep", namespace="ns", skip_port_forward=True)
    with (
        patch.object(TimeTrigger, "wait", lambda self, ctx, stop=None: False),
        patch.object(
            GenerateLoadFault,
            "inject",
            side_effect=AssertionError("a skipped trigger must never inject"),
        ),
    ):
        manager.run_chaos_and_verification(spec, _build_ctx())

    chaos_report, _ = manager.get_reports()
    assert chaos_report["metrics"]["ttd_status"] == "skipped"
    assert chaos_report["metrics"]["ttr_status"] == "skipped"
    assert spec.detect.waits == []


def test_recovery_timing_is_measured_from_injection_and_named_on_the_report() -> None:
    """A converged recovery check stamps TTR; the entries it scored are recorded."""
    spec = ChaosSpec.model_validate(
        {
            "name": "Watched Disruption",
            "trigger": {"type": "time", "delay_seconds": 0},
            "action": {
                "type": "generate_load",
                "target": {"service_url": "http://example.svc", "qps": 50},
            },
            "verify": "planned-verify",
            "recovery_verify": ["web_healthy", "api_healthy"],
        }
    )
    entry = SimpleNamespace(check=object(), resolved_mode="converge")

    manager = ScenarioManager(
        target_deployment="dep",
        namespace="ns",
        verification_mapping={"planned-verify": entry},
        skip_port_forward=True,
    )
    with (
        patch.object(TimeTrigger, "wait", lambda self, ctx, stop=None: True),
        patch.object(GenerateLoadFault, "inject", _inject_at(time.time())),
        patch.object(
            VerifierAgent,
            "run_entry",
            return_value=VerificationResult(success=True, elapsed_time=1.0, reason="back"),
        ),
    ):
        manager.run_chaos_and_verification(spec, _build_ctx())

    chaos_report, _ = manager.get_reports()
    assert chaos_report["metrics"]["ttr_status"] == "recovered"
    assert chaos_report["metrics"]["ttr_seconds"] is not None
    # The remediation-accuracy metric reads these names off the report rather
    # than re-deriving them from the spec it never sees.
    assert chaos_report["recovery_entries"] == ["web_healthy", "api_healthy"]


def test_failing_converge_recovery_is_censored_not_a_recovery_time() -> None:
    spec = _build_spec(verify_key="planned-verify")
    entry = SimpleNamespace(check=object(), resolved_mode="converge")

    manager = ScenarioManager(
        target_deployment="dep",
        namespace="ns",
        verification_mapping={"planned-verify": entry},
        skip_port_forward=True,
    )
    with (
        patch.object(TimeTrigger, "wait", lambda self, ctx, stop=None: True),
        patch.object(GenerateLoadFault, "inject", _inject_at(time.time())),
        patch.object(
            VerifierAgent,
            "run_entry",
            return_value=VerificationResult(success=False, elapsed_time=120.0, reason="still down"),
        ),
    ):
        manager.run_chaos_and_verification(spec, _build_ctx())

    chaos_report, _ = manager.get_reports()
    assert chaos_report["metrics"]["ttr_status"] == "censored"
    # Emphatically not 120.0: that is how long we watched, not how long it took.
    assert chaos_report["metrics"]["ttr_seconds"] is None
    # ``recovery_verify`` unset falls back to the single ``verify`` reference.
    assert chaos_report["recovery_entries"] == ["planned-verify"]


def test_agent_span_is_recorded_on_the_timeline() -> None:
    """Reader can tell a fault the agent was present for from one injected alone."""
    manager = ScenarioManager(target_deployment="dep", namespace="ns", skip_port_forward=True)
    manager.mark_agent_started()
    first = manager.timeline.agent_started_at
    # Idempotent: a re-stamp would move an anchor other numbers derive from.
    manager.mark_agent_started()
    manager.notify_agent_done()

    assert manager.timeline.agent_started_at == first
    assert manager.timeline.agent_finished_at is not None
    assert manager._detect_stop.is_set()  # noqa: SLF001


def test_stop_releases_the_detection_watcher() -> None:
    manager = ScenarioManager(target_deployment="dep", namespace="ns", skip_port_forward=True)
    manager.stop()

    assert manager._detect_stop.is_set()  # noqa: SLF001


@pytest.fixture(autouse=True)
def _no_real_kubectl(monkeypatch: pytest.MonkeyPatch) -> None:
    """Guard against this file accidentally shelling out to ``kubectl``.

    Tests that don't exercise the port-forward path run with
    ``skip_port_forward=True`` so the load fault never opens a tunnel; this guard
    patches the ``subprocess.Popen`` the port-forward helper would use so a test
    that forgets the flag (and isn't deliberately driving the port-forward path)
    fails loudly instead of attempting a real port-forward.

    The one-shot ``kubectl`` path is blocked for the same reason. It is not
    only the fault that can reach it: the detection watcher polls the blast
    radius on its own thread, so a test driving a real fault has a second way
    to shell out.
    """

    def _boom(*args, **kwargs):  # pragma: no cover - exercised only on regression
        raise RuntimeError("test attempted to spawn a real kubectl process")

    monkeypatch.setattr("devops_bench.k8s.kubectl.subprocess.Popen", _boom)
    monkeypatch.setattr("devops_bench.k8s.kubectl.run", _boom)
