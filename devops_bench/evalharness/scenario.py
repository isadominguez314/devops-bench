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

"""Background chaos + verification on a daemon thread.

The :class:`ScenarioManager` resolves the target Service's external
LoadBalancer IP, points the load action at it, threads the port-forward target
env onto the run context as a fallback, and runs chaos plus verification on a
daemon thread, resolving :attr:`ChaosSpec.verify` against a name-keyed
verification mapping supplied by the caller.
"""

from __future__ import annotations

import contextlib
import copy
import socket
import threading
import time
from typing import Any

from devops_bench.chaos import ChaosResult, ChaosSpec
from devops_bench.chaos.faults.generate_load import (
    _ENV_LOCAL_PORT,
    _ENV_SKIP_PORT_FORWARD,
    _ENV_TARGET_DEPLOYMENT,
    _ENV_TARGET_NAMESPACE,
    _LOCAL_PORT,
)
from devops_bench.chaos.timeline import ChaosTimeline, derive_timing_metrics
from devops_bench.core import get_logger
from devops_bench.core.config import get_int
from devops_bench.core.context import RunContext
from devops_bench.k8s import get_resource, poll_until
from devops_bench.verification import VerificationEntry, VerifierAgent

__all__ = [
    "ScenarioManager",
    "VERIFICATION_TIMEOUT_SEC",
    "VERIFICATION_TOTAL_BUDGET_SEC",
    "pick_free_port",
]

_log = get_logger("evalharness.scenario")

# Per-entry budget for a converging entry: how long a single entry's (possibly
# nested) checks may poll before giving up. Assert-mode entries ignore this,
# since single_shot always evaluates once with a zero budget regardless.
VERIFICATION_TIMEOUT_SEC = 120

# Total wall-clock budget for the whole post-run verification pass, across
# every entry. Without a cap, a task with many failing converge objectives
# burns entries x VERIFICATION_TIMEOUT_SEC (12 entries x 120s is 22+ minutes);
# this bounds the pass as a whole. Assert-mode entries still always run, since
# a safeguard that goes unchecked defeats the point of having it.
VERIFICATION_TOTAL_BUDGET_SEC = 600

# Seconds to wait for the target Service's external LoadBalancer IP to be
# assigned by the cloud provider's load balancer controller. LB provisioning
# typically completes within a minute but can lag, so this bounds the wait
# so a stuck assignment falls back to the port-forward path instead of
# stalling the run.
_LB_IP_TIMEOUT_SEC = 180

# Budget for joining the detection-watcher thread once it has been told to
# stop. The watcher's own wait is interruptible (it polls the stop event), so
# this only covers an in-flight ``kubectl get``; it is deliberately short,
# since a wedged watcher must never delay the run's result drain.
_DETECT_JOIN_SEC = 35.0

# Hard cap on how long the scenario thread will wait for the detection watcher
# while an agent is still running. In a normal run this never binds:
# ``notify_agent_done`` releases the watcher the moment the agent exits. It
# exists so a harness that dies without signalling cannot hang the scenario for
# the watcher trigger's own timeout — 30 minutes, for ``agent_action``.
_DETECT_WINDOW_SEC = get_int("CHAOS_DETECT_WINDOW_SEC", 1800)

# How long to wait for the agent's start to be signalled before concluding that
# nobody is going to. Covers only the race where a fault injects and verifies in
# milliseconds, reaching finalization before the harness stamps the agent's
# start; a caller driving the manager with no agent at all pays this once and
# then closes the window.
_AGENT_START_GRACE_SEC = 5.0


def pick_free_port() -> int:
    """Return an ephemeral TCP port currently free on the loopback interface.

    Binds to port 0 and reads back the kernel-assigned port. There is an
    inherent (small) race between releasing the probe socket and ``kubectl``
    binding the port; callers accept it as the cost of avoiding a fixed-port
    collision across concurrent runs.

    Returns:
        A port number that was free at probe time.
    """
    with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.bind(("", 0))
        return sock.getsockname()[1]


class ScenarioManager:
    """Orchestrate a background chaos disruption and its verification.

    The manager runs on a daemon thread alongside the agent under test: it
    waits on the typed trigger, drives the typed
    :class:`~devops_bench.chaos.base.Fault` (via ``action.inject``) to inject
    the planned disruption, then resolves the spec's ``verify:`` key against the
    per-task verification mapping and runs
    :meth:`~devops_bench.verification.VerifierAgent.run_entry` on the resolved
    entry, so the entry's resolved mode (``converge`` vs ``assert``) governs
    whether the check polls or evaluates once, exactly as it does in the
    post-run verification pass. The load fault reaches its target through the
    target Service's external LoadBalancer IP — the manager resolves it from the
    Service status and rewrites the action's load URL before injection — so the
    fortio spike works from any runner location (in-VPC bastion or off-VPC
    local). The ``kubectl port-forward`` the fault still owns is kept as a
    fallback for when LB resolution fails or the caller opts out.

    Args:
        target_deployment: Deployment the load fault should disrupt. Also the
            Service name (the optimize-scale stack seeds the Service with the
            same name), used here to look up the external LB IP and threaded
            onto ``ctx.env`` for the fault's port-forward fallback.
        namespace: Namespace the deployment / Service lives in; threaded onto
            ``ctx.env``.
        verification_mapping: Name-keyed mapping of
            :class:`~devops_bench.verification.spec.VerificationEntry` the
            chaos ``verify:`` reference is resolved against. The manager looks
            up the entry by name and hands it directly to
            ``VerifierAgent.run_entry``, so the entry's resolved mode governs
            the check. Empty mapping disables verification lookups.
        skip_port_forward: When True, the fault runs without resolving an LB IP
            and without opening a ``kubectl port-forward``. The E2E smoke
            harness (against :class:`~devops_bench.deployers.NoOpDeployer`)
            flips this on so tests can exercise the wiring without a real
            cluster.

    Attributes:
        chaos_active_event: Set by the chaos fault once the disruption is
            observably active, so the harness can synchronize the operator
            agent's start.
    """

    def __init__(
        self,
        target_deployment: str,
        namespace: str,
        verification_mapping: dict[str, VerificationEntry] | None = None,
        *,
        skip_port_forward: bool = False,
        local_port: int | None = None,
    ) -> None:
        self.target_deployment = target_deployment
        self.namespace = namespace
        self.verification_mapping: dict[str, VerificationEntry] = dict(verification_mapping or {})
        self.skip_port_forward = skip_port_forward
        # Per-run local port for the fault's port-forward; None keeps the
        # fault's default. Parallel runs pass a free port to avoid contention.
        self.local_port = local_port
        self.chaos_active_event = threading.Event()
        self.verifier_agent = VerifierAgent()
        self.result_holder: dict[str, dict[str, Any]] = {
            "chaos_report": {},
            "perf_report": {},
        }
        self.start_time: float | None = None
        # Wall-clock anchors for the run's timing metrics. Written under the
        # report lock alongside ``result_holder``, since the harness may
        # snapshot the report while this thread is still filling it in.
        self.timeline = ChaosTimeline()
        # Detection watcher state: a background trigger that fires when the
        # agent first mutates the fault's blast radius.
        self._detect_thread: threading.Thread | None = None
        self._detect_stop = threading.Event()
        self._detect_configured = False
        self._detect_fired = False
        # Set by ``mark_agent_started``. Read by ``_await_detection_watch`` to
        # decide whether there is an agent whose lifetime the detection window
        # should track; an event (not just the timeline stamp) so the scenario
        # thread can block on it briefly rather than poll.
        self._agent_started = threading.Event()
        self._aborted = threading.Event()
        # Set when the trigger's outcome can no longer matter: the agent
        # finished (``notify_agent_done``) or the run is aborting (``stop``).
        # Passed to ``Trigger.wait`` so a condition that never fires returns
        # promptly instead of blocking the result drain.
        self._trigger_stop = threading.Event()
        # Guards writes to ``result_holder`` against a concurrent snapshot in
        # ``get_reports`` — the reader can run while this thread is still writing
        # (the timeout path in the harness drains reports without a completed join).
        self._report_lock = threading.Lock()

    def run_chaos_and_verification(
        self,
        spec: ChaosSpec,
        ctx: RunContext,
    ) -> None:
        """Inject the planned fault, then gather verification metrics.

        Args:
            spec: A typed :class:`ChaosSpec` carrying the trigger, action, and
                opaque ``verify:`` key to resolve.
            ctx: The per-task run context (forwarded to the trigger / fault).
        """
        self.start_time = time.time()

        # Record initial chaos metadata before injection begins, so a crash
        # mid-injection still produces a partial report rather than silence.
        with self._report_lock:
            self.timeline.scenario_started_at = self.start_time
            self.result_holder["chaos_report"] = {
                "injected_fault": spec.action.type,
                "name": spec.name,
                "status": "initiated",
                "timeline": self.timeline.to_dict(),
            }

        try:
            chaos_result = self._inject_chaos(spec, ctx)
            if chaos_result is None:
                # The trigger gave up (stopped early or timed out): the fault
                # was never injected, which is a distinct outcome from a
                # failed injection — record it as such and skip verification,
                # which would otherwise measure a disruption that never
                # happened.
                _log.info("chaos trigger did not fire; skipping fault %r", spec.action.type)
                with self._report_lock:
                    self.result_holder["chaos_report"] = {
                        "injected_fault": spec.action.type,
                        "name": spec.name,
                        "status": "skipped",
                        "reason": "trigger did not fire before the agent finished",
                        "recovery_entries": spec.recovery_entries(),
                        "timeline": self.timeline.to_dict(),
                        "metrics": self._timing_metrics(injected=False),
                    }
                # Unblock any chaos-active waiter, exactly as the failure
                # path does: nothing will ever set the event via the fault.
                self.chaos_active_event.set()
                return
            with self._report_lock:
                self.timeline.injected_at = chaos_result.injected_at
                self.timeline.reverted_at = chaos_result.reverted_at
                self.result_holder["chaos_report"] = self._chaos_report_from_result(
                    spec, chaos_result
                )
                # Re-stamped here, not only in ``_finalize_report``: the report
                # dict was just replaced wholesale, and a run whose drain times
                # out never reaches finalization — it should still carry the
                # anchors it had at the cutoff.
                self.result_holder["chaos_report"]["timeline"] = self.timeline.to_dict()
            # Start watching for the agent's first corrective action only once
            # the disruption is real: a watcher armed earlier would baseline
            # against pre-fault state and fire on the agent's *pre-existing*
            # work, reporting a detection that predates the thing detected.
            if chaos_result.success:
                self._start_detection_watch(spec, ctx)
        except Exception as exc:  # noqa: BLE001 - surface failure into the report
            _log.error("error running scenario: %s", exc)
            with self._report_lock:
                self.result_holder["chaos_report"]["status"] = "failed"
                self.result_holder["chaos_report"]["error"] = str(exc)
            # Outside the lock above: ``_finalize_report`` takes it itself, and
            # ``_report_lock`` is a plain (non-reentrant) Lock.
            self._finalize_report(spec, injected=False)
            # Unblock the main thread immediately: it waits on this event to
            # learn the disruption is active, and a failed injection never sets
            # it via the fault, so without this it stalls for the full
            # ``_CHAOS_ACTIVE_WAIT_SEC`` timeout before proceeding.
            self.chaos_active_event.set()
            return

        # A fault that returned ``success=False`` never disrupted anything, so
        # its timing metrics are unavailable rather than zero — even though
        # verification below still runs, exactly as it did before, so the
        # report says what state the workload was left in.
        injected = chaos_result.success

        if self._aborted.is_set():
            self._finalize_report(spec, injected=injected)
            return

        recovery_status, recovery_mode = self._run_recovery_verification(spec)
        self._finalize_report(
            spec,
            injected=injected,
            recovery_status=recovery_status,
            recovery_mode=recovery_mode,
        )

    def _run_recovery_verification(self, spec: ChaosSpec) -> tuple[str | None, str | None]:
        """Run the in-window recovery check and stamp when it converged.

        Args:
            spec: The chaos spec whose ``verify`` key names the check.

        Returns:
            A ``(status, mode)`` pair feeding
            :func:`~devops_bench.chaos.timeline.derive_timing_metrics`:
            the check's tri-state ``status`` and its resolved mode, or
            ``(None, None)`` when no check was scheduled. The mode is what
            separates a *censored* time-to-recovery (a converge check that ran
            out of window) from a definite *not recovered* (an assert check
            that observed the workload still broken).
        """
        verification_entry = self._resolve_verification(spec.verify)
        if verification_entry is None:
            # No verification scheduled (``verify`` was None) — leave the
            # chaos_report alone. An UNKNOWN key, on the other hand, has
            # already stamped ``chaos_report["verification"]`` with the
            # failure record so the operator sees the typo'd cross-reference
            # on the run record, not just in the log.
            return None, None

        mode = verification_entry.resolved_mode
        _log.info("starting planned verification using VerifierAgent...")
        try:
            verification_result = self.verifier_agent.run_entry(
                verification_entry, timeout_sec=VERIFICATION_TIMEOUT_SEC
            )
            _log.info(
                "verification completed: %s",
                verification_result.model_dump_json(indent=2),
            )
            with self._report_lock:
                # Stamped on success only. A converge check returns the moment
                # its condition holds, so "now" is the recovery instant to
                # within one poll interval; on failure there is no instant to
                # record, and inventing one from the deadline would report the
                # window length as the recovery time.
                if verification_result.success:
                    self.timeline.recovery_observed_at = time.time()
                self.result_holder["chaos_report"]["verification"] = (
                    verification_result.model_dump()
                )
                self.result_holder["perf_report"] = self._perf_from_verification(
                    verification_result
                )
            return verification_result.status, mode
        except Exception as exc:  # noqa: BLE001 - surface failure into the report
            _log.error("verification failed with exception: %s", exc)
            with self._report_lock:
                self.result_holder["chaos_report"]["verification"] = {
                    "success": False,
                    "reason": f"Verification exception: {exc}",
                }
            # "error", not "fail": the check could not be evaluated, which is
            # an unmeasured recovery rather than an unrecovered workload.
            return "error", mode

    # -- detection watcher -------------------------------------------------

    def _start_detection_watch(self, spec: ChaosSpec, ctx: RunContext) -> None:
        """Arm the background watcher that anchors time-to-detection.

        The watcher is just a :class:`~devops_bench.chaos.base.Trigger` run on
        its own daemon thread: whatever condition would have *fired* a fault is
        equally able to *timestamp* the agent's response, so the watcher axis
        inherits every registered trigger (and every externally-registered one)
        for free. It stops when the agent finishes or the run aborts, so a run
        whose agent never touches the blast radius records "not observed"
        rather than holding the drain open.

        Failures are swallowed into "no watcher": a broken detection metric
        must never cost the run its chaos result.

        Args:
            spec: The chaos spec, consulted for a ``detect:`` override before
                falling back to the fault's own ``detection_watch()``.
            ctx: Run context handed to the watcher's ``wait``.
        """
        try:
            watcher = spec.detection_trigger()
        except Exception:  # noqa: BLE001 - a bad watcher must not sink the run
            _log.exception("failed to build the chaos detection watcher; TTD unavailable")
            return
        if watcher is None:
            _log.info(
                "fault %r declares no detection watch and the spec sets no 'detect' "
                "node; time-to-detection will be recorded as unavailable",
                spec.action.type,
            )
            return

        def _watch() -> None:
            try:
                fired = watcher.wait(ctx, stop=self._detect_stop)
            except Exception:  # noqa: BLE001 - watcher errors are not run errors
                _log.exception("chaos detection watcher crashed; TTD unavailable")
                return
            if fired is False:
                return
            with self._report_lock:
                self._detect_fired = True
                self.timeline.first_action_at = time.time()
            _log.info("chaos detection watcher fired: agent touched the blast radius")

        self._detect_configured = True
        self._detect_thread = threading.Thread(target=_watch, daemon=True)
        self._detect_thread.start()

    def _timing_metrics(
        self,
        *,
        injected: bool,
        recovery_status: str | None = None,
        recovery_mode: str | None = None,
    ) -> dict[str, Any]:
        """Derive the timing metrics from the current timeline.

        Callers must already hold :attr:`_report_lock` (or be past the point
        where the watcher thread can still write), since this reads the
        timeline and the watcher flags.
        """
        return derive_timing_metrics(
            self.timeline,
            injected=injected,
            detection_configured=self._detect_configured,
            detection_fired=self._detect_fired,
            recovery_status=recovery_status,  # type: ignore[arg-type]
            recovery_mode=recovery_mode,
        )

    def _finalize_report(
        self,
        spec: ChaosSpec,
        *,
        injected: bool,
        recovery_status: str | None = None,
        recovery_mode: str | None = None,
    ) -> None:
        """Settle the watcher, then stamp the timeline + metrics onto the report.

        The watcher is resolved *before* the metrics are derived: leaving it
        running would let a late mutation land a ``first_action_at`` after the
        snapshot was taken, so a reader could see ``ttd_status: not_observed``
        next to a populated ``first_action_at``.

        "Settle" means *wait*, not *cut short*. The detection window belongs to
        the agent's lifetime, not to the in-window recovery check's — an agent
        that diagnoses for three minutes and then fixes the workload has
        detected it, and stopping the watcher when verification's 120s budget
        expired would silently record that run as "never responded".
        """
        self._await_detection_watch()
        with self._report_lock:
            report = self.result_holder.setdefault("chaos_report", {})
            report["recovery_entries"] = spec.recovery_entries()
            report["timeline"] = self.timeline.to_dict()
            report["metrics"] = self._timing_metrics(
                injected=injected,
                recovery_status=recovery_status,
                recovery_mode=recovery_mode,
            )

    def _await_detection_watch(self) -> None:
        """Wait for the watcher to fire or be released, then stop it.

        The detection window is the *agent's* lifetime, so this holds the
        scenario thread open only while an agent is actually running: the
        watcher thread exits when its condition fires or when
        ``notify_agent_done`` / ``stop`` sets the stop event, and
        :data:`_DETECT_WINDOW_SEC` bounds the wait so a harness that dies
        without signalling cannot hang the run.

        When no agent start was ever signalled there is nothing to wait *for* —
        a watcher polling on behalf of an agent that does not exist can only
        report the "not observed" it already knows — so the window closes
        immediately rather than blocking for the watcher trigger's own timeout.
        """
        thread = self._detect_thread
        if thread is None:
            return
        if not thread.is_alive() or self._detect_stop.is_set():
            # Nothing left to wait for: the watcher already finished (it fired,
            # or its own condition gave up), or it has been released because
            # the agent finished / the run is aborting. The join below collects
            # it as soon as it notices.
            pass
        elif self._agent_started.wait(timeout=_AGENT_START_GRACE_SEC):
            thread.join(timeout=_DETECT_WINDOW_SEC)
            if thread.is_alive():
                _log.warning(
                    "chaos detection watcher still running after %ss; "
                    "closing the window and recording what it saw",
                    _DETECT_WINDOW_SEC,
                )
        else:
            _log.info(
                "no agent start was signalled within %ss; closing the detection "
                "window now instead of holding the run open for the watcher",
                _AGENT_START_GRACE_SEC,
            )
        self._stop_detection_watch()

    def _stop_detection_watch(self, timeout: float = _DETECT_JOIN_SEC) -> None:
        """Signal the watcher to stop and join it. Idempotent and never raises."""
        self._detect_stop.set()
        thread = self._detect_thread
        if thread is not None:
            thread.join(timeout=timeout)

    def _inject_chaos(self, spec: ChaosSpec, ctx: RunContext) -> ChaosResult | None:
        """Wait on the trigger, then drive ``action.inject`` with the target env.

        The trigger is a typed node; wait through its own ``wait(ctx, stop)``
        rather than reading raw ``delay_seconds`` here — the harness only knows
        the ``Trigger`` Protocol, not the concrete trigger's parameters. Before
        injecting, the manager resolves the target Service's external
        LoadBalancer IP and points the action's load URL at
        ``http://<lb-ip>:8080`` (so the fortio spike hits the workload directly
        from any runner location); the port-forward target is also threaded onto
        ``ctx.env`` as a fallback for when LB resolution fails. When
        ``skip_port_forward`` is True (E2E smoke / no real cluster), the LB
        resolution is skipped and the fault runs against whatever URL the
        action already carries. Both are load-URL concerns, so the whole block
        is gated on the action carrying a ``target.service_url`` — a fault
        without one (e.g. ``kill_pod``) must not spend up to the LB timeout
        resolving an IP it will never use.

        Args:
            spec: Typed chaos spec.
            ctx: Run context handed to the trigger / fault.

        Returns:
            The :class:`~devops_bench.chaos.ChaosResult` returned by the fault,
            or ``None`` when the trigger declined to fire (stopped early or
            timed out) and the fault was skipped. Only an explicit ``False``
            skips: legacy triggers returning ``None`` still fire.
        """
        fired = spec.trigger.wait(ctx, stop=self._trigger_stop)
        if fired is False:
            return None
        with self._report_lock:
            self.timeline.trigger_fired_at = time.time()

        # Thread the port-forward target onto the context. ``ctx.env`` values
        # are strings; flags are written only when truthy so the fault's
        # ``bool(env.get(...))`` reads cleanly.
        ctx.env[_ENV_TARGET_DEPLOYMENT] = self.target_deployment
        ctx.env[_ENV_TARGET_NAMESPACE] = self.namespace
        if self.local_port is not None:
            ctx.env[_ENV_LOCAL_PORT] = str(self.local_port)

        # Only actions that carry a load URL need a route to the workload.
        load_target = getattr(spec.action, "target", None)
        has_load_url = hasattr(load_target, "service_url")

        if self.skip_port_forward:
            # Smoke / no-cluster path: there is no cluster to query for an LB
            # IP, so leave the action's URL alone and just flag the fault to
            # skip the tunnel.
            ctx.env[_ENV_SKIP_PORT_FORWARD] = "1"
        elif has_load_url:
            is_local = ctx.cluster is not None and ctx.cluster.location == "local"
            if is_local:
                _log.info(
                    "local cluster (location=%r) detected; skipping LoadBalancer IP resolution "
                    "to rely directly on port-forward fallback",
                    ctx.cluster.location,
                )
                lb_ip = None
            else:
                # Real-cluster path: resolve the external LB IP and rewrite the
                # action's load URL to point at it directly. Fall back to the
                # port-forward path if resolution fails or times out — that way a
                # delayed LB still produces a load attempt instead of an aborted
                # run.
                lb_ip = self._resolve_lb_ip(self.target_deployment, self.namespace)

            if lb_ip is not None:
                lb_url = f"http://{lb_ip}:{_LOCAL_PORT}"
                _log.info(
                    "chaos load will hit external LB %s (no port-forward)",
                    lb_url,
                )
                load_target.service_url = lb_url
                ctx.env[_ENV_SKIP_PORT_FORWARD] = "1"
            # If lb_ip is None (timeout / local skip / kubectl error) the skip env is left
            # unset; the fault then opens its own port-forward as the fallback.

        return spec.action.inject(ctx, self.chaos_active_event)

    @staticmethod
    def _resolve_lb_ip(service: str, namespace: str) -> str | None:
        """Poll the target Service for an external LoadBalancer IP.

        Reads ``status.loadBalancer.ingress[0].ip`` (falling back to
        ``hostname`` when the cloud provider hands back a DNS name instead of an
        IP) and waits up to :data:`_LB_IP_TIMEOUT_SEC` for it to appear, since
        LoadBalancer provisioning may take a minute or two to set up the
        underlying network LB and firewall rule.

        Args:
            service: Service name (the optimize-scale target Service is named
                after the target deployment, so the manager reuses
                ``target_deployment`` here).
            namespace: Namespace the Service lives in.

        Returns:
            The external IP/hostname as a string, or ``None`` if the timeout
            elapses or kubectl fails. The caller treats ``None`` as a signal to
            fall back to the port-forward transport.
        """
        resolved: dict[str, str] = {}

        def _has_ip() -> bool:
            try:
                doc = get_resource("service", service, namespace=namespace)
            except Exception as exc:  # noqa: BLE001 - poll keeps retrying
                _log.debug("waiting for LB IP on %s/%s: %s", namespace, service, exc)
                return False
            ingress = (doc.get("status") or {}).get("loadBalancer", {}).get("ingress") or []
            if not ingress:
                return False
            entry = ingress[0] or {}
            ip = entry.get("ip") or entry.get("hostname")
            if not ip:
                return False
            resolved["ip"] = ip
            return True

        _log.info(
            "resolving external LB IP for service %s/%s (timeout %ss)",
            namespace,
            service,
            _LB_IP_TIMEOUT_SEC,
        )
        ok = poll_until(_has_ip, timeout_sec=_LB_IP_TIMEOUT_SEC)
        if not ok:
            _log.warning(
                "external LB IP for service %s/%s not assigned within %ss; "
                "falling back to port-forward",
                namespace,
                service,
                _LB_IP_TIMEOUT_SEC,
            )
            return None
        return resolved["ip"]

    @staticmethod
    def _chaos_report_from_result(spec: ChaosSpec, result: Any) -> dict[str, Any]:
        """Shape a typed ``ChaosResult`` into the chaos-report dict.

        Args:
            spec: The originating spec (carries the human-readable ``name``).
            result: A :class:`~devops_bench.chaos.ChaosResult`.

        Returns:
            The ``chaos_report`` dict consumed by the result reporter; the
            ``status`` field is derived from ``ChaosResult.success``.
        """
        dumped = result.model_dump()
        # Carry both the human-readable name and the typed result fields so
        # downstream consumers see a superset of both.
        report: dict[str, Any] = {
            "injected_fault": result.injected_fault,
            "name": spec.name,
            "status": "success" if result.success else "failed",
            "output": dumped.get("output", ""),
            "elapsed_time": dumped.get("elapsed_time", 0.0),
        }
        if dumped.get("error") is not None:
            report["error"] = dumped["error"]
        return report

    @staticmethod
    def _perf_from_verification(result: Any) -> dict[str, Any]:
        """Derive ``perf_report`` from a verification result.

        Deployment time flows through on success; the uptime / utilization
        fields collapse to a success binary.
        """
        success = bool(result.success)
        elapsed = float(result.elapsed_time)
        return {
            "deployment_time_seconds": elapsed if success else None,
            "uptime_percentage": 100.0 if success else 0.0,
            "resource_utilization_efficiency": 1.0 if success else 0.0,
        }

    def _resolve_verification(self, verify_ref: str | None) -> VerificationEntry | None:
        """Resolve the chaos spec's opaque ``verify`` key against the mapping.

        Args:
            verify_ref: The string key carried on :attr:`ChaosSpec.verify`, or
                ``None`` when the spec opts out of verification.

        Returns:
            The mapped :class:`~devops_bench.verification.spec.VerificationEntry`
            (already validated) when the key is present and known; ``None``
            when the spec opts out **or** the key is unknown. The caller hands
            the entry to ``VerifierAgent.run_entry`` unmodified, so the
            entry's resolved mode (``converge`` vs ``assert``) governs the
            check rather than being decided here. The unknown-key case is
            *not* silent — a verification-failure entry is written into
            ``chaos_report`` naming the missing key + the available keys, so a
            typo'd cross-reference shows up on the run record (not just in
            the log).
        """
        if not verify_ref:
            return None
        entry = self.verification_mapping.get(verify_ref)
        if entry is None:
            known = sorted(self.verification_mapping.keys())
            reason = (
                f"chaos verify reference {verify_ref!r} not found in "
                f"verification mapping; known keys: {known}"
            )
            _log.warning(reason)
            # Surface the unresolved reference on the chaos_report so the
            # operator sees the typo'd cross-reference in results.json, not
            # just in the log. The shape mirrors the typed
            # VerificationResult dump (success/reason/name) so downstream
            # consumers don't need a special-case parse path.
            with self._report_lock:
                self.result_holder["chaos_report"]["verification"] = {
                    "success": False,
                    "reason": reason,
                    "name": verify_ref,
                    "unresolved_reference": verify_ref,
                    "known_references": known,
                }
            return None
        return entry

    def get_reports(self) -> tuple[dict[str, Any], dict[str, Any]]:
        """Return the aggregated chaos and performance reports.

        Returns:
            A ``(chaos_report, perf_report)`` pair derived from the most recent
            scenario run; each is an empty dict before the run produces it. The
            snapshot is a deep copy taken under the report lock, so a caller may
            read it safely while the scenario thread is still writing.
        """
        with self._report_lock:
            return (
                copy.deepcopy(self.result_holder.get("chaos_report", {})),
                copy.deepcopy(self.result_holder.get("perf_report", {})),
            )

    def mark_agent_started(self) -> None:
        """Stamp the moment the operator agent under test was launched.

        Recorded so a reader can tell a fault the agent was present for from
        one injected into an empty room, and so detection time can be read
        relative to the agent's own start when a trigger fires before it.
        Thread-safe and idempotent — the first call wins, since a re-stamp
        would move an anchor other numbers are already derived from.
        """
        with self._report_lock:
            if self.timeline.agent_started_at is None:
                self.timeline.agent_started_at = time.time()
        self._agent_started.set()

    def notify_agent_done(self) -> None:
        """Signal that the agent has finished, releasing a still-waiting trigger.

        A trigger whose condition depends on the agent (or simply has not
        fired yet) can never usefully fire once the agent has exited; setting
        the trigger-stop event makes its ``wait`` return promptly so the
        scenario records ``status: "skipped"`` instead of blocking the result
        drain until the join budget expires. A trigger that already fired is
        unaffected — the fault and its verification run to completion.

        The detection watcher is released for the same reason: an agent that
        has exited cannot take another corrective action, so a watcher still
        polling would only delay the drain before recording the "not observed"
        it already knows. Thread-safe and idempotent, like :meth:`stop`.
        """
        with self._report_lock:
            if self.timeline.agent_finished_at is None:
                self.timeline.agent_finished_at = time.time()
        self._trigger_stop.set()
        self._detect_stop.set()

    def stop(self) -> None:
        """Abort the scenario so a pending verification is skipped.

        Sets an abort flag the scenario thread checks before dispatching
        verification, the trigger-stop event so a trigger still waiting exits
        promptly, and the detection-watch stop so its thread unwinds. The
        ``kubectl port-forward`` is owned by the load fault (which tears it
        down in its own ``finally``), so there is nothing for the manager to
        release here. Safe to call more than once and from a different thread
        than the scenario's; it never raises, so it can run from a ``finally``
        block during cleanup.
        """
        self._aborted.set()
        self._trigger_stop.set()
        self._detect_stop.set()
