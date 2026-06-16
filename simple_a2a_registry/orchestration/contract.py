"""Agent Runtime Contract — core data models for interaction governance.

Defines the contract layer (§7 field agreement) that every interaction
between agents must conform to: InteractionMode, TaskEnvelope,
SecurityContext, and OutputContract.

All types use plain ``dataclasses`` (no Pydantic) following the project
convention.  Every dataclass provides ``to_dict()`` / ``from_dict()``
for JSON serialization.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class InteractionMode(str, Enum):
    """How an interaction is governed by the runtime contract.

    Values:
        SYNC_CALL — function-level, atomic, synchronous return; does not
                    enter the state machine.
        TASK      — job-level, asynchronous, claim-based; enters the
                    state machine.
        JOB       — project-level, decomposable into a sub-task DAG.
    """

    SYNC_CALL = "sync_call"
    TASK = "task"
    JOB = "job"


# ---------------------------------------------------------------------------
# SecurityContext
# ---------------------------------------------------------------------------


@dataclass
class SecurityContext:
    """Security metadata attached to every task envelope.

    Fields:
        effective_scope:    Resolved authorization scope (e.g.
                            "agent:read").
        delegation_depth:   How many hops deep this task is in the
                            delegation chain (0 = origin).
        deadline_ms:        Unix timestamp in milliseconds — the task
                            MUST complete or be cancelled by this time.
        provenance_chain_id:  Immutable identifier linking all tasks
                              in the same provenance chain.
    """

    effective_scope: str = ""
    delegation_depth: int = 0
    deadline_ms: int = 0
    provenance_chain_id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a plain dict, dropping None fields."""
        return _dataclass_to_dict(self)

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> SecurityContext:
        """Deserialize from a plain dict."""
        valid = {
            k: v for k, v in data.items()
            if k in SecurityContext.__dataclass_fields__
        }
        return SecurityContext(**valid)


# ---------------------------------------------------------------------------
# OutputContract
# ---------------------------------------------------------------------------


@dataclass
class OutputContract:
    """Describes the expected output shape for a task.

    Fields:
        required_fields:  List of field names that MUST be present in
                          the output payload.
    """

    required_fields: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a plain dict, dropping None fields."""
        return _dataclass_to_dict(self)

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> OutputContract:
        """Deserialize from a plain dict."""
        valid = {
            k: v for k, v in data.items()
            if k in OutputContract.__dataclass_fields__
        }
        return OutputContract(**valid)


# ---------------------------------------------------------------------------
# TaskEnvelope
# ---------------------------------------------------------------------------


@dataclass
class TaskEnvelope:
    """The universal wrapper for every interaction between agents.

    References the 7+2 field agreement from the Agent Runtime Contract
    design doc (§7).

    Fields (7 required + 2 optional):
        task_id            Unique identifier for this task (REQUIRED).
        interaction_mode   How this interaction is governed (REQUIRED).
        skill              The target skill name (REQUIRED).
        input_schema       Optional JSON Schema for input validation.
        input              The actual input payload (REQUIRED).
        output_contract    Expected output shape (REQUIRED).
        security_context   Security metadata (REQUIRED).
        tenant_id          Tenant namespace (REQUIRED).
        workspace_uri      Optional URI for the agent's workspace.
    """

    task_id: str = ""
    interaction_mode: InteractionMode = InteractionMode.TASK
    skill: str = ""
    input_schema: Optional[Dict[str, Any]] = None
    input: Dict[str, Any] = field(default_factory=dict)
    output_contract: Optional[OutputContract] = None
    security_context: Optional[SecurityContext] = None
    tenant_id: str = ""
    workspace_uri: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a plain dict, dropping None fields.

        Nested dataclass objects (output_contract, security_context) are
        recursively serialized.  The interaction_mode enum is converted
        to its string value.
        """
        d: Dict[str, Any] = {}
        for f_name in self.__dataclass_fields__:
            val = getattr(self, f_name)
            if val is None:
                continue
            if f_name == "interaction_mode":
                d[f_name] = val.value
            elif hasattr(val, "__dataclass_fields__"):
                d[f_name] = _dataclass_to_dict(val)
            else:
                d[f_name] = val
        return d

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> TaskEnvelope:
        """Deserialize from a plain dict.

        Nested dicts for output_contract and security_context are
        converted back to their respective dataclass types.
        """
        # Handle interaction_mode enum
        mode_raw = data.get("interaction_mode", "task")
        if isinstance(mode_raw, str):
            interaction_mode = InteractionMode(mode_raw)
        else:
            interaction_mode = mode_raw

        # Handle nested dataclasses
        output_contract = None
        oc_data = data.get("output_contract")
        if isinstance(oc_data, dict):
            output_contract = OutputContract.from_dict(oc_data)

        security_context = None
        sc_data = data.get("security_context")
        if isinstance(sc_data, dict):
            security_context = SecurityContext.from_dict(sc_data)

        return TaskEnvelope(
            task_id=data.get("task_id", ""),
            interaction_mode=interaction_mode,
            skill=data.get("skill", ""),
            input_schema=data.get("input_schema"),
            input=data.get("input", {}),
            output_contract=output_contract,
            security_context=security_context,
            tenant_id=data.get("tenant_id", ""),
            workspace_uri=data.get("workspace_uri"),
        )


# ---------------------------------------------------------------------------
# Internal serialization helper
# ---------------------------------------------------------------------------


def _dataclass_to_dict(obj: Any) -> Dict[str, Any]:
    """Recursively convert a dataclass tree to a plain dict, dropping None.

    Handles nested dataclass instances and lists of dataclass instances.
    """
    result: Dict[str, Any] = {}
    for f_name, f_type in obj.__dataclass_fields__.items():
        value = getattr(obj, f_name)
        if value is None:
            continue
        if isinstance(value, list):
            result[f_name] = [
                _dataclass_to_dict(v) if hasattr(v, "__dataclass_fields__") else v
                for v in value
            ]
        elif isinstance(value, dict):
            result[f_name] = {
                k: _dataclass_to_dict(v) if hasattr(v, "__dataclass_fields__") else v
                for k, v in value.items()
            }
        elif hasattr(value, "__dataclass_fields__"):
            result[f_name] = _dataclass_to_dict(value)
        else:
            result[f_name] = value
    return result