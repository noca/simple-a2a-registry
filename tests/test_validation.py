"""Table-driven tests for OutputContract validator (T4).

Covers all AC-04 branches:
  1. Missing required fields → fail
  2. All required fields present → pass
  3. Extra fields present → pass (don't reject extra)
  4. Non-dict output → fail
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import pytest

from simple_a2a_registry.orchestration.contract import OutputContract
from simple_a2a_registry.orchestration.validation import validate_output


# ---------------------------------------------------------------------------
# Table-driven test: AC-04 all branches
# ---------------------------------------------------------------------------


def _case(
    output: Any,
    required_fields: List[str],
    expected_valid: bool,
    expected_error_substring: Optional[str] = None,
    case_id: str = "",
) -> dict:
    return {
        "output": output,
        "contract": OutputContract(required_fields=required_fields),
        "expected_valid": expected_valid,
        "expected_error_substring": expected_error_substring,
        "case_id": case_id,
    }


VALIDATION_CASES: List[dict] = [
    # AC-04.1: Missing required fields → fail
    _case(
        output={"status": "ok"},
        required_fields=["result", "status"],
        expected_valid=False,
        expected_error_substring="missing fields: result",
        case_id="missing_one_field",
    ),
    _case(
        output={"a": 1},
        required_fields=["a", "b", "c"],
        expected_valid=False,
        expected_error_substring="missing fields: b, c",
        case_id="missing_two_fields",
    ),
    _case(
        output={},
        required_fields=["anything"],
        expected_valid=False,
        expected_error_substring="missing fields: anything",
        case_id="empty_output_missing_field",
    ),
    # AC-04.2: All required fields present → pass
    _case(
        output={"result": "ok", "status": "done"},
        required_fields=["result", "status"],
        expected_valid=True,
        case_id="all_fields_present",
    ),
    _case(
        output={"a": 1, "b": 2, "c": 3},
        required_fields=["a", "b", "c"],
        expected_valid=True,
        case_id="all_three_fields",
    ),
    # AC-04.3: Empty required list → always pass
    _case(
        output={},
        required_fields=[],
        expected_valid=True,
        case_id="empty_contract_empty_output",
    ),
    _case(
        output={"anything": 42},
        required_fields=[],
        expected_valid=True,
        case_id="empty_contract_with_output",
    ),
    # AC-04.4: Extra fields → pass (don't reject extra)
    _case(
        output={"result": "ok", "status": "done", "extra": "ignored"},
        required_fields=["result"],
        expected_valid=True,
        case_id="extra_fields_allowed",
    ),
    _case(
        output={"a": 1, "b": 2, "c": 3, "d": 4, "e": 5},
        required_fields=["a", "b"],
        expected_valid=True,
        case_id="many_extra_fields",
    ),
    # Non-dict output: fail
    _case(
        output="not_a_dict",
        required_fields=["x"],
        expected_valid=False,
        expected_error_substring="output must be a dict",
        case_id="string_output",
    ),
    _case(
        output=None,
        required_fields=["x"],
        expected_valid=False,
        expected_error_substring="output must be a dict",
        case_id="none_output",
    ),
    _case(
        output=42,
        required_fields=[],
        expected_valid=False,
        expected_error_substring="output must be a dict",
        case_id="int_output_empty_required",
    ),
    _case(
        output=["list", "not", "dict"],
        required_fields=["x"],
        expected_valid=False,
        expected_error_substring="output must be a dict",
        case_id="list_output",
    ),
    # Edge: required field present with None value → pass (presence, not truthiness)
    _case(
        output={"nullable": None},
        required_fields=["nullable"],
        expected_valid=True,
        case_id="none_value_still_present",
    ),
    # Edge: nested dict as single required field
    _case(
        output={"nested": {"inner": "value"}},
        required_fields=["nested"],
        expected_valid=True,
        case_id="nested_dict_field",
    ),
]


class TestValidateOutputTableDriven:
    """Table-driven tests covering all AC-04 branches."""

    @pytest.mark.parametrize(
        "case",
        VALIDATION_CASES,
        ids=lambda c: c["case_id"],
    )
    def test_validate_output(self, case: dict) -> None:
        valid, err = validate_output(case["output"], case["contract"])
        assert valid == case["expected_valid"]
        if case["expected_valid"]:
            assert err is None
        else:
            assert err is not None
            sub = case.get("expected_error_substring")
            if sub:
                assert sub in err, f"Expected substring '{sub}' in error '{err}'"


# ---------------------------------------------------------------------------
# Non-dict output: concise table with multiple input shapes
# ---------------------------------------------------------------------------

NON_DICT_CASES: List[Tuple[Any, str]] = [
    (None, "None"),
    ("string", "string"),
    (42, "int"),
    (3.14, "float"),
    ([1, 2, 3], "list"),
    (True, "bool"),
    (set(), "set"),
    ((1,), "tuple"),
]


class TestValidateOutputNonDict:
    """All non-dict types must fail."""

    @pytest.mark.parametrize(
        "bad_output,label",
        NON_DICT_CASES,
    )
    def test_non_dict_fails(self, bad_output: Any, label: str) -> None:
        contract = OutputContract(required_fields=["field"])
        valid, err = validate_output(bad_output, contract)
        assert not valid
        assert "output must be a dict" in (err or "")

    def test_non_dict_empty_required_still_fails(self) -> None:
        """Even with empty required_fields, non-dict output fails."""
        valid, err = validate_output("not_a_dict", OutputContract(required_fields=[]))
        assert not valid
        assert "output must be a dict" in (err or "")