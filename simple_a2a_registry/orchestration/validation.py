"""Output contract validation — required field checking for task results.

§6 SCN-04, AC-04, D4: Only required fields validation, no full JSON Schema.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from simple_a2a_registry.orchestration.contract import OutputContract


def validate_output(
    output: Any,
    contract: OutputContract,
) -> Tuple[bool, Optional[str]]:
    """Validate *output* against *contract* required fields.

    Only checks that all required fields are present (D4 decision).
    Does NOT perform full JSON Schema validation (YAGNI).

    Args:
        output:     The actual output payload (expected to be a dict).
        contract:   The OutputContract specifying required fields.

    Returns:
        (True, None)                          if validation passes.
        (False, "missing fields: field1, ...") if validation fails.
    """
    if not isinstance(output, dict):
        return (False, "output must be a dict")

    required = contract.required_fields
    if not required:
        return (True, None)

    missing = [f for f in required if f not in output]
    if missing:
        return (False, f"missing fields: {', '.join(missing)}")

    return (True, None)