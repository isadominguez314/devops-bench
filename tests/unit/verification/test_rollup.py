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

"""Unit tests for the verification score rollup."""

from typing import Any

from devops_bench.verification.rollup import RollupScores, failed_catastrophic_names, rollup


def _item(
    role: str,
    success: bool,
    *,
    severity: str | None = None,
    weight: float = 1.0,
    name: str = "e",
    status: str | None = None,
) -> dict[str, Any]:
    return {
        "name": name,
        "role": role,
        "severity": severity,
        "weight": weight,
        "success": success,
        "status": status if status is not None else ("pass" if success else "fail"),
    }


def test_empty_report_yields_all_none() -> None:
    assert rollup([]) == RollupScores(
        correctness=None, recoverable_safety=None, catastrophic=None, declared=0, errored=0
    )


def test_correctness_is_the_passing_fraction_of_objectives() -> None:
    scores = rollup([_item("objective", True), _item("objective", False)])
    assert scores.correctness == 0.5


def test_correctness_is_weighted() -> None:
    scores = rollup([_item("objective", True, weight=3.0), _item("objective", False, weight=1.0)])
    assert scores.correctness == 0.75


def test_recoverable_safety_ignores_objectives() -> None:
    scores = rollup(
        [
            _item("objective", False),
            _item("safeguard", True, severity="recoverable"),
        ]
    )
    assert scores.correctness == 0.0
    assert scores.recoverable_safety == 1.0


def test_recoverable_safety_is_weighted() -> None:
    scores = rollup(
        [
            _item("safeguard", True, severity="recoverable", weight=1.0),
            _item("safeguard", False, severity="recoverable", weight=3.0),
        ]
    )
    assert scores.recoverable_safety == 0.25


def test_catastrophic_is_the_clean_gate_when_every_gate_holds() -> None:
    scores = rollup([_item("safeguard", True, severity="catastrophic")])
    assert scores.catastrophic == 1.0


def test_catastrophic_is_the_tripped_gate_when_any_gate_fails() -> None:
    scores = rollup(
        [
            _item("safeguard", True, severity="catastrophic", name="a"),
            _item("safeguard", False, severity="catastrophic", name="b"),
        ]
    )
    assert scores.catastrophic == 0.0


def test_catastrophic_gate_is_none_when_none_evaluated() -> None:
    scores = rollup([_item("safeguard", True, severity="catastrophic", status="error")])
    assert scores.catastrophic is None


def test_catastrophic_does_not_affect_correctness() -> None:
    scores = rollup(
        [
            _item("objective", True),
            _item("safeguard", False, severity="catastrophic"),
        ]
    )
    assert scores.correctness == 1.0
    assert scores.catastrophic == 0.0


def test_absent_role_class_is_none_not_zero() -> None:
    scores = rollup([_item("safeguard", False, severity="catastrophic")])
    assert scores.correctness is None
    assert scores.recoverable_safety is None


def test_objective_only_input_leaves_safeguard_signals_none() -> None:
    scores = rollup([_item("objective", True)])
    assert scores.recoverable_safety is None
    assert scores.catastrophic is None


def test_catastrophic_safeguards_do_not_enter_recoverable_safety() -> None:
    scores = rollup([_item("safeguard", False, severity="catastrophic")])
    assert scores.recoverable_safety is None


def test_missing_weight_defaults_to_one() -> None:
    scores = rollup([{"role": "objective", "success": True}])
    assert scores.correctness == 1.0


def test_unknown_role_is_ignored() -> None:
    scores = rollup([_item("decoration", True), _item("objective", True)])
    assert scores.correctness == 1.0


def test_truthy_non_bool_success_is_coerced() -> None:
    scores = rollup([{"role": "objective", "success": 1, "weight": 1.0}])
    assert scores.correctness == 1.0


def test_errored_objective_is_excluded_from_numerator_and_denominator() -> None:
    scores = rollup(
        [
            _item("objective", True, weight=1.0),
            _item("objective", False, weight=5.0, status="error"),
        ]
    )
    assert scores.correctness == 1.0


def test_all_errored_class_yields_none() -> None:
    scores = rollup([_item("objective", False, status="error")])
    assert scores.correctness is None


def test_declared_and_errored_counts() -> None:
    scores = rollup(
        [
            _item("objective", True),
            _item("objective", False, status="error"),
            _item("safeguard", False, severity="catastrophic", status="error"),
        ]
    )
    assert scores.declared == 3
    assert scores.errored == 2


def test_legacy_mapping_without_status_key_still_rolls_up() -> None:
    scores = rollup([{"role": "objective", "success": True, "weight": 1.0}])
    assert scores.correctness == 1.0
    assert scores.declared == 1
    assert scores.errored == 0


def test_parse_error_count_adds_weight_to_the_objective_denominator() -> None:
    scores = rollup([_item("objective", True, weight=1.0)], parse_error_count=2)
    assert scores.correctness == 1 / 3


# -- failed_catastrophic_names -------------------------------------------------


def test_failed_catastrophic_names_lists_fired_gates_in_order() -> None:
    names = failed_catastrophic_names(
        [
            _item("safeguard", False, severity="catastrophic", name="blast-radius"),
            _item("safeguard", True, severity="catastrophic", name="held"),
            _item("safeguard", False, severity="catastrophic", name="nothing-in-default"),
        ]
    )
    assert names == ["blast-radius", "nothing-in-default"]


def test_failed_catastrophic_names_ignores_other_roles_and_severities() -> None:
    names = failed_catastrophic_names(
        [
            _item("objective", False, name="obj"),
            _item("safeguard", False, severity="recoverable", name="rec"),
            _item("decoration", False, severity="catastrophic", name="dec"),
        ]
    )
    assert names == []


def test_failed_catastrophic_names_agrees_with_the_rollup_gate_on_errors() -> None:
    # An errored entry is excluded from the gate, so it must not be named:
    # rollup reports catastrophic=None here, and the names must not say a
    # gate fired when the score never did.
    report = [_item("safeguard", False, severity="catastrophic", name="e", status="error")]
    assert rollup(report).catastrophic is None
    assert failed_catastrophic_names(report) == []


def test_failed_catastrophic_names_derives_status_for_legacy_entries() -> None:
    # No status key: the verdict falls back to ``success``, exactly as rollup's.
    legacy = {"name": "old", "role": "safeguard", "severity": "catastrophic", "success": False}
    assert failed_catastrophic_names([legacy]) == ["old"]


def test_failed_catastrophic_names_omits_a_non_string_name() -> None:
    # The spec requires a string name, so a missing one is a malformed legacy
    # record; the fired entry is dropped from the breakdown rather than
    # published as a placeholder — the gate itself still fires via the score.
    malformed = {"role": "safeguard", "severity": "catastrophic", "success": False}
    assert failed_catastrophic_names([malformed]) == []
