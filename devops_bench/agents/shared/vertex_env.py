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

"""Vertex routing env shared by the CLI agents (Gemini, antigravity).

Both harnesses resolve the same question — which Vertex location to route a
keyless run at — and drifted apart while doing it. One implementation here so
they cannot drift again. Importing this module pulls no provider SDK.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing-only import
    from collections.abc import Callable

__all__ = ["DEFAULT_VERTEX_LOCATION", "VERTEX_LOCATION_ENVS", "vertex_location"]

# ``global`` rather than a region: the ``-preview`` Gemini ids this benchmark
# defaults to (``gemini-3.1-pro-preview``) are only published on the global
# endpoint and return 404 from a regional one. Matches the default already used
# by devops_bench/models/{gemini,claude}.py and the claude_code harness.
DEFAULT_VERTEX_LOCATION = "global"

# Highest precedence first.
#
# ``GCP_VERTEX_LOCATION`` is the repo-wide spelling (models/gemini.py,
# models/claude.py, the claude_code harness); it was missing from this chain, so
# an operator who set the documented variable had it silently ignored here.
#
# ``GCP_LOCATION`` stays last and is deliberately *below* the Vertex-specific
# name: deployers/factory.py reads it as a cluster **zone** (``us-central1-a``),
# which is not a valid Vertex location at all. Anyone who set it for their
# cluster should not thereby repoint model traffic.
VERTEX_LOCATION_ENVS = (
    "GOOGLE_CLOUD_LOCATION",
    "GCP_VERTEX_LOCATION",
    "GCP_LOCATION",
)


def vertex_location(
    *,
    fallback: Callable[[], str | None] | None = None,
    default: str = DEFAULT_VERTEX_LOCATION,
) -> str:
    """Resolve the Vertex location for a keyless run.

    Args:
        fallback: Optional last-resort lookup (e.g. querying gcloud), consulted
            only when every variable in :data:`VERTEX_LOCATION_ENVS` is unset.
            Passed as a callable rather than a value so a caller whose lookup
            shells out does not pay for it on the common configured path.
        default: Value returned when neither the env chain nor ``fallback``
            yields anything.

    Returns:
        The first non-empty value from :data:`VERTEX_LOCATION_ENVS`, else
        ``fallback()``, else ``default``. Values are stripped, so a variable set
        to whitespace is treated as unset rather than routing traffic at ``" "``.
    """
    for name in VERTEX_LOCATION_ENVS:
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    if fallback is not None:
        value = (fallback() or "").strip()
        if value:
            return value
    return default
