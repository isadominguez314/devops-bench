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

"""Result model, abstract bases, and the :data:`FAULTS` / :data:`TRIGGERS` registries."""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from typing import ClassVar

from pydantic import BaseModel

from devops_bench.core import Registry
from devops_bench.core.context import RunContext

__all__ = [
    "ChaosResult",
    "Fault",
    "Trigger",
    "FAULTS",
    "TRIGGERS",
]

#: Registry of concrete :class:`Fault` subclasses, keyed by their ``type``.
#: ``entry_point_group`` lets external packages register a fault without
#: touching this tree.
FAULTS: Registry[type[Fault]] = Registry("faults", entry_point_group="devops_bench.faults")

#: Registry of concrete :class:`Trigger` subclasses, keyed by their ``type``.
TRIGGERS: Registry[type[Trigger]] = Registry("triggers", entry_point_group="devops_bench.triggers")


class ChaosResult(BaseModel):
    """Structured outcome of a chaos fault injection.

    Attributes:
        success: True when the fault completed without raising.
        injected_fault: Fault id (``Fault.type``) — keys diagnosis scoring in
            metrics.
        output: Free-form text payload (typically the model's final summary).
        elapsed_time: Wall-clock seconds spent injecting the fault.
        injected_at: UTC epoch seconds at the moment the disruption became
            real (the API call that mutates the cluster returned), not when
            ``inject`` was entered — a fault that spends 30s resolving its
            target must not charge that setup to the agent's detection time.
            ``None`` when the fault does not stamp it; every timing metric
            derived from it degrades to "unavailable" rather than guessing.
        reverted_at: UTC epoch seconds at which a self-reverting fault undid
            its disruption. ``None`` for faults whose disruption is a
            one-shot event (``kill_pod``) or which had not reverted when the
            result was built.
        error: Human-readable error string when ``success`` is False; ``None``
            on success.
    """

    success: bool
    injected_fault: str
    output: str = ""
    elapsed_time: float = 0.0
    injected_at: float | None = None
    reverted_at: float | None = None
    error: str | None = None


class Fault(BaseModel, ABC):
    """Abstract base for a ``type``-tagged chaos fault node.

    Concrete faults are pydantic models that carry their own typed parameters
    plus a ``type: Literal["..."]`` discriminator and self-register under that
    key via ``@FAULTS.register(...)``. They implement :meth:`inject`, which
    drives the disruption against the cluster described by ``ctx`` and returns
    a typed :class:`ChaosResult`.
    """

    @abstractmethod
    def inject(
        self,
        ctx: RunContext,
        chaos_active_event: threading.Event | None = None,
    ) -> ChaosResult:
        """Inject the fault and return its structured outcome.

        Args:
            ctx: Run context describing the target cluster and workspace.
            chaos_active_event: Optional event the fault sets when the
                disruption is observably active (e.g. load is flowing), so the
                harness can coordinate measurements. ``None`` disables the
                signal.

        Returns:
            A :class:`ChaosResult` describing the injection outcome.
        """
        raise NotImplementedError

    def detection_watch(self) -> Trigger | None:
        """Return a trigger that fires on the agent's first corrective action.

        The benchmark cannot observe the agent *noticing* a fault — the agent
        is an external process and its reads leave no trace. What it can
        observe is the first mutation of the blast radius by something other
        than the fault, which is the proxy the time-to-detection metric is
        built on. A fault that knows its own target returns a trigger watching
        it; the scenario runs that trigger on a background thread from
        ``injected_at`` and stamps the moment it fires.

        Watch a *spec* field (``metadata.generation`` on a Deployment), never
        ``status`` or a bare ``resourceVersion``: controller churn following
        the injection bumps those on its own and would score the fault's own
        blast wave as the agent's response.

        Returns:
            A :class:`Trigger` to run as the detection watcher, or ``None``
            when the fault declares no blast radius — the scenario then
            records the metric as unavailable instead of inventing one. A
            spec-level ``detect:`` node overrides whatever this returns.
        """
        return None


class Trigger(BaseModel, ABC):
    """Abstract base for a ``type``-tagged chaos firing condition.

    Concrete triggers are pydantic models with a ``type: Literal["..."]``
    discriminator and self-register via ``@TRIGGERS.register(...)``. They
    implement :meth:`wait`, which blocks until the condition the trigger
    encodes is met — or until it can no longer fire, in which case the fault
    is skipped rather than injected.
    """

    #: Triggers that observe the *agent's* effects (e.g. a resource mutation
    #: the agent performs) can only fire while the agent is running. The
    #: harness skips its pre-agent chaos-active gate for these, since waiting
    #: for the disruption to be active before starting the agent would
    #: deadlock on a condition only the agent can satisfy.
    requires_agent_running: ClassVar[bool] = False

    @abstractmethod
    def wait(self, ctx: RunContext, stop: threading.Event | None = None) -> bool:
        """Block until the trigger's condition is satisfied or abandoned.

        Args:
            ctx: Run context describing the target cluster and workspace.
            stop: Optional event the caller sets when the trigger's outcome can
                no longer matter (the agent finished, or the run is aborting).
                A trigger should return promptly once it is set.

        Returns:
            True when the condition fired and the fault should inject; False
            when the trigger gave up (stopped early or timed out) and the
            fault should be skipped. Callers treat a legacy ``None`` return as
            True so pre-existing external triggers keep working.
        """
        raise NotImplementedError
