"""Flow Controller — per-agent concurrency limits, circuit breaker, and retry backoff.

Provides a :class:`FlowController` that the :class:`Dispatcher` uses to
decide whether a task can be dispatched to a particular agent at a given
moment.

Three mechanisms cooperate:

1. **Max concurrent tasks** — cap the number of simultaneously dispatched
   tasks per agent.  Extra tasks stay ``ready`` and will be picked up in
   a future poll cycle.

2. **Circuit breaker** — when an agent accumulates N consecutive failures
   (dispatch failures, not task results), dispatch is paused for a cooldown
   period, then automatically resumes.

3. **Retry backoff** — provides the recommended delay between retry attempts
   as ``next_retry_at`` timestamps so the store can delay promotion.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Dict, Optional

logger = logging.getLogger("a2a_registry.orchestration.flow_control")


# ---------------------------------------------------------------------------
# Per-agent flow state
# ---------------------------------------------------------------------------


@dataclass
class AgentFlowState:
    """Mutable runtime state for one agent.

    Thread-safe only if accessed from a single asyncio loop (the
    dispatcher's poll loop).
    """

    concurrent_count: int = 0
    consecutive_failures: int = 0
    circuit_tripped_until: float = 0.0  # unix timestamp, 0 = not tripped


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass
class FlowControlConfig:
    """Configuration for the flow controller.

    Attributes:
        max_concurrent_tasks:
            Maximum number of tasks that can be dispatched to a single agent
            simultaneously.  0 means unlimited.
        circuit_breaker_threshold:
            Consecutive dispatching failures before the circuit breaker trips.
        circuit_breaker_cooldown:
            Seconds to wait before automatically resetting the circuit breaker.
        retry_backoff_base:
            Base delay in seconds for exponential retry backoff.  The actual
            delay is ``min(retry_backoff_base * (2 ** failure_count), max_backoff)``.
        retry_backoff_max:
            Maximum backoff delay in seconds (capped exponential growth).
    """

    max_concurrent_tasks: int = 5
    circuit_breaker_threshold: int = 3
    circuit_breaker_cooldown: int = 300  # 5 minutes
    retry_backoff_base: int = 30
    retry_backoff_max: int = 3600  # 1 hour


# ---------------------------------------------------------------------------
# FlowController
# ---------------------------------------------------------------------------


class FlowController:
    """In-memory flow control for per-agent dispatch decisions.

    The controller is designed to be used exclusively by the background
    dispatcher poll loop (single-threaded async).  It is NOT thread-safe;
    callers from outside the poll loop must provide their own locking.

    Usage::

        fc = FlowController(FlowControlConfig(max_concurrent_tasks=3))

        agent = "worker-1"

        # Check before dispatching
        if fc.can_dispatch(agent):
            fc.on_task_dispatched(agent)
            # ... actually dispatch ...
            # On success (callback ack, WS send ok):
            fc.on_task_arrived(agent)
            # On failure:
            fc.on_task_failed(agent)
            fc.on_task_departed(agent)  # decrement count

        # When a task completes normally (via result handler):
        fc.on_task_completed(agent)
        fc.on_task_departed(agent)
    """

    def __init__(self, config: Optional[FlowControlConfig] = None) -> None:
        self.config = config or FlowControlConfig()
        self._agents: Dict[str, AgentFlowState] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def can_dispatch(self, agent_id: str) -> bool:
        """Check if a task can be dispatched to *agent_id* right now.

        Returns ``False`` if:
        - The agent's circuit breaker is tripped.
        - The agent has reached ``max_concurrent_tasks``.

        Otherwise returns ``True``.
        """
        state = self._agents.get(agent_id)
        if state is None:
            return True  # first dispatch, no state yet

        # Circuit breaker check
        if self._is_circuit_tripped(state):
            remaining = state.circuit_tripped_until - time.time()
            logger.info(
                "Circuit breaker tripped for agent '%s' — "
                "pausing dispatch for %.0f more seconds",
                agent_id, max(0, remaining),
            )
            return False

        # Concurrency limit check
        if self.config.max_concurrent_tasks > 0:
            if state.concurrent_count >= self.config.max_concurrent_tasks:
                logger.debug(
                    "Agent '%s' at max concurrent tasks (%d/%d)",
                    agent_id, state.concurrent_count,
                    self.config.max_concurrent_tasks,
                )
                return False

        return True

    def on_task_dispatched(self, agent_id: str) -> None:
        """Record that a task has been dispatched to *agent_id*.

        This increases the concurrent count (the permit is taken).
        Call BEFORE the actual dispatch attempt so the permit slot is
        consumed atomically with the decision.
        """
        state = self._agents.setdefault(agent_id, AgentFlowState())
        state.concurrent_count += 1
        logger.debug(
            "Task dispatched to agent '%s' — concurrent=%d",
            agent_id, state.concurrent_count,
        )

    def on_task_arrived(self, agent_id: str) -> None:
        """Record that a task successfully arrived at the agent.

        This resets the consecutive failure counter.  Call when the
        dispatch mechanism (WS send / HTTP callback) succeeds.
        """
        state = self._agents.get(agent_id)
        if state is None:
            return
        state.consecutive_failures = 0
        logger.debug(
            "Task arrived at agent '%s' — failures reset",
            agent_id,
        )

    def on_task_failed(self, agent_id: str) -> None:
        """Record a dispatch failure for *agent_id*.

        If the consecutive failure count reaches the circuit breaker
        threshold, the circuit trips for ``circuit_breaker_cooldown``
        seconds.
        """
        state = self._agents.setdefault(agent_id, AgentFlowState())
        state.consecutive_failures += 1
        failures = state.consecutive_failures

        logger.info(
            "Dispatch failure for agent '%s' — consecutive_failures=%d/%d",
            agent_id, failures, self.config.circuit_breaker_threshold,
        )

        if failures >= self.config.circuit_breaker_threshold:
            self._trip_circuit(state)
            logger.warning(
                "Circuit breaker TRIPPED for agent '%s' — "
                "pausing dispatch for %ds (threshold=%d)",
                agent_id,
                self.config.circuit_breaker_cooldown,
                self.config.circuit_breaker_threshold,
            )

    def on_task_completed(self, agent_id: str) -> None:
        """Record a successful task completion for *agent_id*.

        Resets the consecutive failure counter.  Call when a dispatched
        task completes successfully (via result handler / callback ack).
        """
        state = self._agents.get(agent_id)
        if state is None:
            return
        state.consecutive_failures = 0
        logger.debug(
            "Task completed for agent '%s' — failures reset",
            agent_id,
        )

    def on_task_departed(self, agent_id: str) -> None:
        """Record that a task is no longer occupying a dispatch slot.

        Decrements the concurrent count.  Call when a dispatched task
        finishes (success or failure) or the dispatch attempt itself
        failed.
        """
        state = self._agents.get(agent_id)
        if state is None:
            return
        state.concurrent_count = max(0, state.concurrent_count - 1)
        logger.debug(
            "Task departed from agent '%s' — concurrent=%d",
            agent_id, state.concurrent_count,
        )

    # ------------------------------------------------------------------
    # Circuit breaker helpers
    # ------------------------------------------------------------------

    def is_circuit_tripped(self, agent_id: str) -> bool:
        """Check if the circuit breaker is currently tripped for *agent_id*."""
        state = self._agents.get(agent_id)
        if state is None:
            return False
        return self._is_circuit_tripped(state)

    def _is_circuit_tripped(self, state: AgentFlowState) -> bool:
        """Check if *state*'s circuit is currently tripped.

        Auto-resets if the cooldown has elapsed.
        """
        if state.circuit_tripped_until == 0:
            return False
        if time.time() >= state.circuit_tripped_until:
            state.circuit_tripped_until = 0
            state.consecutive_failures = 0
            logger.info("Circuit breaker auto-reset")
            return False
        return True

    def _trip_circuit(self, state: AgentFlowState) -> None:
        """Trip the circuit breaker for *state*."""
        state.circuit_tripped_until = time.time() + self.config.circuit_breaker_cooldown

    def reset_circuit(self, agent_id: str) -> None:
        """Manually reset the circuit breaker for *agent_id*."""
        state = self._agents.get(agent_id)
        if state is None:
            return
        state.circuit_tripped_until = 0
        state.consecutive_failures = 0
        logger.info("Circuit breaker manually reset for agent '%s'", agent_id)

    # ------------------------------------------------------------------
    # Retry backoff
    # ------------------------------------------------------------------

    def get_retry_backoff(self, failure_count: int) -> float:
        """Calculate the backoff delay for the *failure_count*-th retry.

        Uses exponential backoff::

            delay = backoff_base * (2 ** (failure_count - 1))

        Capped at ``retry_backoff_max``.

        Args:
            failure_count: How many times the task has already failed
                (1-indexed — first failure uses ``backoff_base``).

        Returns:
            Delay in seconds before the next retry should be attempted.
        """
        delay = float(self.config.retry_backoff_base)
        for _ in range(failure_count - 1):
            delay *= 2.0
        return min(delay, float(self.config.retry_backoff_max))

    def get_concurrent_count(self, agent_id: str) -> int:
        """Return the current concurrent dispatch count for *agent_id*."""
        state = self._agents.get(agent_id)
        if state is None:
            return 0
        return state.concurrent_count

    def get_consecutive_failures(self, agent_id: str) -> int:
        """Return the current consecutive failure count for *agent_id*."""
        state = self._agents.get(agent_id)
        if state is None:
            return 0
        return state.consecutive_failures

    def get_remaining_cooldown(self, agent_id: str) -> float:
        """Return the remaining cooldown seconds for *agent_id*, or 0."""
        state = self._agents.get(agent_id)
        if state is None:
            return 0.0
        if state.circuit_tripped_until == 0:
            return 0.0
        remaining = state.circuit_tripped_until - time.time()
        return max(0.0, remaining)

    def reset(self) -> None:
        """Reset all agent state (concurrent counts, failures, circuits)."""
        self._agents.clear()
        logger.info("FlowController state reset")
