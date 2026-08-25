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

"""The ``kill_pod`` fault: terminate a workload's pods and let controllers react.

Unlike ``generate_load``, this fault is mechanical — :meth:`KillPodFault.inject`
never touches the chaos LLM agent. It resolves the target pods (from a
deployment's own selector or an explicit label selector), deletes them by name
via ``kubectl delete``, and returns. Deletion is graceful and non-blocking:
the point is the disruption plus the controller's recreation, not SIGKILL
semantics, and whether the replacement pods actually come back healthy is the
job of the spec's ``verify:`` entry, not of the fault.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Literal

from pydantic import BaseModel, model_validator

from devops_bench.chaos.base import FAULTS, ChaosResult, Fault, Trigger
from devops_bench.chaos.triggers.agent_action import AgentActionTrigger
from devops_bench.core import get_logger
from devops_bench.core.context import RunContext
from devops_bench.k8s import delete_resource, get_resource

__all__ = ["KillPodFault", "PodTarget"]

_log = get_logger("chaos.kill_pod")

# Bound on each kubectl call so a wedged API server fails the fault instead of
# hanging the scenario thread.
_KUBECTL_TIMEOUT_SEC = 60


class PodTarget(BaseModel):
    """Target for a pod-kill disruption.

    Exactly one of ``deployment`` / ``selector`` selects the pods:
    ``deployment`` resolves the Deployment's own ``spec.selector.matchLabels``
    at injection time (so the fault tracks whatever labels the workload
    actually carries), while ``selector`` is used verbatim.

    Attributes:
        deployment: Deployment whose pods to kill; its selector is resolved
            at injection time.
        selector: Explicit label selector (``-l``) identifying the pods.
        namespace: Namespace the pods live in.
    """

    deployment: str | None = None
    selector: str | None = None
    namespace: str = "default"

    @model_validator(mode="after")
    def _exactly_one_selection(self) -> PodTarget:
        """Reject targets that name both or neither selection mechanism."""
        if bool(self.deployment) == bool(self.selector):
            raise ValueError("PodTarget requires exactly one of 'deployment' or 'selector'")
        return self


@FAULTS.register("kill_pod")
class KillPodFault(Fault):
    """Delete the target's pods so their controller must recreate them.

    Attributes:
        type: Discriminator literal, always ``"kill_pod"``.
        target: Typed pod-target description (deployment or selector, plus
            namespace).
    """

    type: Literal["kill_pod"] = "kill_pod"
    target: PodTarget

    def inject(
        self,
        ctx: RunContext,
        chaos_active_event: threading.Event | None = None,
    ) -> ChaosResult:
        """Resolve the target pods, delete them, and report what was killed.

        Fails closed: a selector that matches no pods is a failed injection,
        not a vacuous success — a disruption that touched nothing must not
        read as one the workload survived.

        Args:
            ctx: Run context; ``ctx.cluster`` (when present) supplies the
                kubeconfig for the kubectl calls.
            chaos_active_event: Optional event set once the deletion has been
                accepted, so the harness can observe the disruption is live.

        Returns:
            A :class:`ChaosResult`; exceptions are converted to
            ``success=False`` so one fault never aborts the run.
        """
        start = time.monotonic()
        try:
            selector = self._resolve_selector(ctx)
            pods = get_resource(
                "pods",
                selector=selector,
                namespace=self.target.namespace,
                kubeconfig=ctx.cluster,
                timeout=_KUBECTL_TIMEOUT_SEC,
            )
            names = [
                name
                for item in pods.get("items") or []
                if (name := (item.get("metadata") or {}).get("name"))
            ]
            if not names:
                return ChaosResult(
                    success=False,
                    injected_fault=self.type,
                    elapsed_time=time.monotonic() - start,
                    error=(
                        f"no pods matched selector {selector!r} in "
                        f"namespace {self.target.namespace!r}"
                    ),
                )

            _log.info(
                "kill_pod deleting %d pod(s) in %s: %s",
                len(names),
                self.target.namespace,
                ", ".join(names),
            )
            delete_resource(
                "pod",
                names,
                namespace=self.target.namespace,
                kubeconfig=ctx.cluster,
                timeout=_KUBECTL_TIMEOUT_SEC,
            )
            # Stamped here, not at the top of ``inject``: the disruption
            # becomes real when the API server accepts the deletion, and the
            # selector resolution above must not be charged to the agent's
            # detection time. ``time.time`` (not ``monotonic``) because this
            # is a wall-clock anchor correlated against other clocks in the
            # report; ``elapsed_time`` keeps using the monotonic span.
            injected_at = time.time()
            if chaos_active_event is not None:
                chaos_active_event.set()
            return ChaosResult(
                success=True,
                injected_fault=self.type,
                output=(
                    f"deleted {len(names)} pod(s) in namespace "
                    f"{self.target.namespace!r}: {', '.join(names)}"
                ),
                elapsed_time=time.monotonic() - start,
                injected_at=injected_at,
            )
        except Exception as exc:  # noqa: BLE001 - one fault must never abort the run
            _log.exception("kill_pod fault crashed")
            return ChaosResult(
                success=False,
                injected_fault=self.type,
                elapsed_time=time.monotonic() - start,
                error=f"{type(exc).__name__}: {exc}",
            )

    def detection_watch(self) -> Trigger | None:
        """Watch the target Deployment's spec for the agent's first response.

        ``metadata.generation`` bumps only when the Deployment's *spec*
        changes — an edit, a ``scale``, a ``rollout restart``. The pod
        recreation this fault provokes is a status-level event driven by the
        ReplicaSet controller and leaves ``generation`` alone, so a change to
        it after ``injected_at`` is attributable to the agent rather than to
        the fault's own blast wave.

        Returns:
            A change-from-baseline :class:`AgentActionTrigger` on the target
            Deployment, or ``None`` for a selector-only target: bare pods
            carry no ``generation``, and the workload's controller is not
            identifiable from a label selector alone.
        """
        if not self.target.deployment:
            return None
        return AgentActionTrigger(
            kind="deployment",
            resource_name=self.target.deployment,
            namespace=self.target.namespace,
            path="metadata.generation",
        )

    def _resolve_selector(self, ctx: RunContext) -> str:
        """Return the label selector identifying the target pods.

        An explicit ``selector`` is used verbatim; otherwise the target
        Deployment's ``spec.selector.matchLabels`` is read and joined as
        ``k=v,k=v``.

        Raises:
            ValueError: If the Deployment carries no ``matchLabels`` (e.g. a
                matchExpressions-only selector, which this fault does not
                translate).
        """
        if self.target.selector:
            return self.target.selector
        doc: dict[str, Any] = get_resource(
            "deployment",
            self.target.deployment,
            namespace=self.target.namespace,
            kubeconfig=ctx.cluster,
            timeout=_KUBECTL_TIMEOUT_SEC,
        )
        match_labels = ((doc.get("spec") or {}).get("selector") or {}).get("matchLabels") or {}
        if not match_labels:
            raise ValueError(
                f"deployment {self.target.namespace}/{self.target.deployment} has no "
                "spec.selector.matchLabels to derive a pod selector from"
            )
        return ",".join(f"{key}={value}" for key, value in sorted(match_labels.items()))
