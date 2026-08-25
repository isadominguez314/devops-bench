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

"""The ``agent_action`` trigger: fires when the agent's mutation becomes visible.

The agent under test is an external CLI process, so its tool calls cannot be
intercepted; the trigger instead observes the *effect* of an action on the
cluster. It polls the watched resource(s) via ``kubectl get -o json`` and fires
when the watched condition is met:

- ``path`` + ``equals``: the value at ``path`` equals ``equals`` (stateless —
  fires immediately if the condition already holds when the wait starts).
- ``path`` alone: the value at ``path`` differs from the baseline captured
  when the wait starts.
- neither: ``metadata.resourceVersion`` differs from the baseline (any
  mutation at all).

Because the condition can only be satisfied while the agent is running, the
trigger declares ``requires_agent_running`` so the harness does not gate the
agent's start on the disruption being active, and it honors the ``stop`` event
so an action the agent never takes skips the fault instead of blocking the
result drain.
"""

from __future__ import annotations

import threading
from functools import lru_cache
from typing import Any, ClassVar, Literal

from jsonpath_ng.ext import parse as _jsonpath_parse
from pydantic import Field

from devops_bench.chaos.base import TRIGGERS, Trigger
from devops_bench.core import get_logger
from devops_bench.core.context import RunContext
from devops_bench.k8s import get_resource, poll_until

__all__ = ["AgentActionTrigger"]

_log = get_logger("chaos.agent_action_trigger")

# Bound on a single ``kubectl get`` so a wedged API server connection cannot
# stall the poll loop past the trigger's own timeout accounting.
_GET_TIMEOUT_SEC = 30

# Poll pacing: the first recheck comes quickly so a fast agent action is
# caught near its occurrence; backoff is capped low because the trigger's
# whole point is timing the injection to the action.
_POLL_INITIAL_DELAY_SEC = 1.0
_POLL_MAX_DELAY_SEC = 5.0


@lru_cache(maxsize=64)
def _compile(path: str) -> Any:
    """Compile a JSONPath expression once and reuse it across poll iterations."""
    return _jsonpath_parse(path)


@TRIGGERS.register("agent_action")
class AgentActionTrigger(Trigger):
    """Fire when the agent's mutation of a watched resource becomes visible.

    Attributes:
        type: Discriminator literal, always ``"agent_action"``.
        kind: Resource kind to watch, e.g. ``"clusterpolicy"``.
        resource_name: Specific object to watch; ``None`` watches every object
            of ``kind`` (list query, keyed by ``metadata.name``) and fires when
            ANY of them satisfies the condition.
        namespace: Namespace for namespaced kinds; omit for cluster-scoped.
        path: Optional JSONPath into each watched object. See the module
            docstring for how ``path`` / ``equals`` select the firing mode.
        equals: Optional value the ``path`` result must equal to fire.
        timeout_seconds: Hard cap on the wait; expiry skips the fault.
    """

    requires_agent_running: ClassVar[bool] = True

    type: Literal["agent_action"] = "agent_action"
    kind: str
    resource_name: str | None = None
    namespace: str | None = None
    path: str | None = None
    equals: Any = None
    timeout_seconds: float = Field(default=1800.0, ge=1.0)

    def wait(self, ctx: RunContext, stop: threading.Event | None = None) -> bool:
        """Poll the watched resource(s) until the condition fires or gives up.

        Args:
            ctx: Run context; ``ctx.cluster`` (when present) supplies the
                kubeconfig for the reads.
            stop: Optional event; set when the agent finished or the run is
                aborting — the condition can then never fire, so return False.

        Returns:
            True when the watched condition was observed; False on timeout or
            stop.
        """
        baseline = self._snapshot(ctx)
        _log.info(
            "agent_action trigger watching %s%s (path=%r, equals=%r, timeout=%ss)",
            self.kind,
            f"/{self.resource_name}" if self.resource_name else " (all)",
            self.path,
            self.equals,
            self.timeout_seconds,
        )

        def _fired() -> bool:
            # Fold the stop signal into the predicate so poll_until returns
            # promptly; the sleep below also wakes on it.
            if stop is not None and stop.is_set():
                return True
            return self._condition_met(ctx, baseline)

        sleep = stop.wait if stop is not None else None
        done = poll_until(
            _fired,
            timeout_sec=self.timeout_seconds,
            initial_delay=_POLL_INITIAL_DELAY_SEC,
            max_delay=_POLL_MAX_DELAY_SEC,
            **({"sleep": sleep} if sleep is not None else {}),
        )
        if stop is not None and stop.is_set():
            _log.info("agent_action trigger stopped before the action was observed; skipping")
            return False
        if not done:
            _log.info(
                "agent_action trigger timed out after %ss without observing the action; skipping",
                self.timeout_seconds,
            )
            return False
        _log.info("agent_action trigger fired: watched condition observed")
        return True

    # -- observation -------------------------------------------------------

    def _read_objects(self, ctx: RunContext) -> list[dict[str, Any]]:
        """Fetch the watched object(s), tolerating transient read failures.

        A resource that does not exist yet, or an API-server blip, reads as
        "no objects" so the poll keeps going rather than crashing the trigger.

        Args:
            ctx: Run context supplying the kubeconfig source.

        Returns:
            The watched objects; a named watch yields a single-element list.
        """
        try:
            doc = get_resource(
                self.kind,
                self.resource_name,
                namespace=self.namespace,
                kubeconfig=ctx.cluster,
                timeout=_GET_TIMEOUT_SEC,
            )
        except Exception as exc:  # noqa: BLE001 - poll keeps retrying
            _log.debug("agent_action read of %s failed (retrying): %s", self.kind, exc)
            return []
        if self.resource_name is not None:
            return [doc]
        items = doc.get("items")
        return items if isinstance(items, list) else []

    def _observe(self, obj: dict[str, Any]) -> Any:
        """Return the watched value for one object.

        With ``path`` set, the value(s) the JSONPath resolves to (``None``
        when it resolves to nothing); otherwise ``metadata.resourceVersion``.
        """
        if self.path is None:
            return (obj.get("metadata") or {}).get("resourceVersion")
        found = [match.value for match in _compile(self.path).find(obj)]
        if not found:
            return None
        return found[0] if len(found) == 1 else found

    def _snapshot(self, ctx: RunContext) -> dict[str, Any]:
        """Capture the per-object baseline the change modes compare against.

        Keyed by ``metadata.name``. Empty when the equals mode is active (it
        is stateless) or when the initial read fails — an object missing from
        the baseline that later appears mutated still fires, which is the
        intended reading (the agent created or changed it).
        """
        if self.equals is not None:
            return {}
        return {
            (obj.get("metadata") or {}).get("name", "<unnamed>"): self._observe(obj)
            for obj in self._read_objects(ctx)
        }

    def _condition_met(self, ctx: RunContext, baseline: dict[str, Any]) -> bool:
        """Evaluate the firing condition against the current cluster state."""
        for obj in self._read_objects(ctx):
            observed = self._observe(obj)
            if self.equals is not None:
                if observed == self.equals:
                    return True
                continue
            name = (obj.get("metadata") or {}).get("name", "<unnamed>")
            if name not in baseline:
                # New object of the watched kind: a mutation by definition.
                return True
            if observed != baseline[name]:
                return True
        return False
