"""Persistent registry state — registration store with heartbeat management."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from simple_a2a_registry.models import AgentCard, Provider

logger = logging.getLogger("a2a_registry.store")

HEARTBEAT_TIMEOUT = 120   # seconds before an agent is considered stale
HEARTBEAT_PURGE = 300     # seconds before a stale agent is fully removed


class A2ARegistryStore:
    """Manages Agent Card registrations with heartbeat-based liveness.

    Agents are registered via :meth:`register_agent` and persisted
    to a JSON file on every write.
    """

    def __init__(self, data_dir: str) -> None:
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._file = self.data_dir / "registry.json"
        self._lock = asyncio.Lock()

        # In-memory state
        self._agents: Dict[str, AgentCard] = {}
        self._heartbeats: Dict[str, float] = {}
        self._discovered: Dict[str, AgentCard] = {}

        self._load()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self) -> None:
        """Load external agent registrations from the JSON file."""
        if not self._file.exists():
            return
        try:
            data = json.loads(self._file.read_text())
            raw_agents: Dict[str, Dict] = data.get("agents", {})
            self._agents = {
                aid: AgentCard.from_dict(card)
                for aid, card in raw_agents.items()
            }
            self._heartbeats = {
                aid: ts
                for aid, ts in data.get("heartbeats", {}).items()
                if isinstance(ts, (int, float))
            }
            raw_discovered: Dict[str, Dict] = data.get("discovered", {})
            self._discovered = {
                aid: AgentCard.from_dict(card)
                for aid, card in raw_discovered.items()
            }
            logger.info(
                "Loaded %d external + %d discovered agent registrations",
                len(self._agents), len(self._discovered),
            )
        except Exception as e:
            logger.warning("Failed to load registry data: %s", e)

    def _save(self) -> None:
        """Persist registrations to disk (atomic write via tmp + replace)."""
        external = {
            aid: card.to_dict()
            for aid, card in self._agents.items()
        }
        discovered = {
            aid: card.to_dict()
            for aid, card in self._discovered.items()
        }
        hb = {aid: ts for aid, ts in self._heartbeats.items()}
        try:
            tmp = self._file.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(
                    {"agents": external, "heartbeats": hb, "discovered": discovered},
                    indent=2,
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            tmp.replace(self._file)
        except Exception as e:
            logger.error("Failed to save registry: %s", e)

    # ------------------------------------------------------------------
    # Discovered agents (e.g. from filesystem scan)
    # ------------------------------------------------------------------

    def set_discovered_agents(self, cards: List[Dict[str, Any]]) -> None:
        """Replace all discovered (filesystem-scanned) agents."""
        self._discovered = {}
        for c in cards:
            card = AgentCard.from_dict(c)
            self._discovered[card.id] = card
        self._save()

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def list_agents(
        self,
        skill: Optional[str] = None,
        tag: Optional[str] = None,
        q: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """List all agents, optionally filtered.

        Args:
            skill: Substring match against skill name or id.
            tag: Exact match against agent tags.
            q: Case-insensitive full-text search across the entire card.

        Returns:
            List of Agent Card dicts with ``status`` and ``lastHeartbeat``.
        """
        now = time.time()
        results: List[Dict[str, Any]] = []

        by_id: Dict[str, Dict] = {}
        for agent_id, card in self._agents.items():
            last_hb = self._heartbeats.get(agent_id)
            elapsed = now - last_hb if last_hb else HEARTBEAT_TIMEOUT + 1
            is_alive = elapsed <= HEARTBEAT_TIMEOUT
            card_dict = card.to_dict()
            card_dict["status"] = "alive" if is_alive else "stale"
            card_dict["lastHeartbeat"] = last_hb
            by_id[agent_id] = card_dict

        for agent_id, card in self._discovered.items():
            if agent_id not in by_id:
                card_dict = card.to_dict()
                card_dict["status"] = "alive"
                card_dict["lastHeartbeat"] = None
                by_id[agent_id] = card_dict

        for agent_id, card_dict in by_id.items():
            if skill:
                skills = card_dict.get("capabilities", {}).get("skills", [])
                if not any(
                    skill in (s.get("name", "") or s.get("id", ""))
                    for s in skills
                ):
                    continue
            if tag:
                if tag not in card_dict.get("tags", []):
                    continue
            if q:
                ql = q.lower()
                haystack = json.dumps(card_dict, ensure_ascii=False).lower()
                if ql not in haystack:
                    continue
            results.append(card_dict)

        return results

    def get_agent(self, agent_id: str) -> Optional[Dict[str, Any]]:
        """Get a single agent's card with live status.

        Returns:
            Agent Card dict with ``status`` and ``lastHeartbeat``,
            or ``None`` if the agent doesn't exist.
        """
        card = self._agents.get(agent_id) or self._discovered.get(agent_id)
        if card is None:
            return None
        last_hb = self._heartbeats.get(agent_id)
        elapsed = time.time() - last_hb if last_hb else HEARTBEAT_TIMEOUT + 1
        card_dict = card.to_dict()
        card_dict["status"] = "alive" if elapsed <= HEARTBEAT_TIMEOUT else "stale"
        card_dict["lastHeartbeat"] = last_hb
        return card_dict

    # ------------------------------------------------------------------
    # Mutations
    # ------------------------------------------------------------------

    def register_agent(self, agent_card: Dict) -> str:
        """Register an external agent and set its first heartbeat.

        Args:
            agent_card: Agent Card dict (as per A2A spec).  An
                ``id`` key is optional — one is generated when missing.

        Returns:
            The assigned agent id.
        """
        card = AgentCard.from_dict(agent_card)
        agent_id = card.ensure_id()

        now_ts = time.time()
        card.metadata = dict(card.metadata)
        card.metadata["registeredAt"] = datetime.now(
            timezone.utc
        ).isoformat()

        self._agents[agent_id] = card
        self._heartbeats[agent_id] = now_ts
        self._save()
        return agent_id

    def heartbeat(self, agent_id: str) -> bool:
        """Record a heartbeat for an agent.

        Args:
            agent_id: The agent's unique identifier.

        Returns:
            ``True`` if the agent is known, ``False`` otherwise.
        """
        if agent_id not in self._agents:
            return False
        self._heartbeats[agent_id] = time.time()
        self._save()
        return True

    def unregister(self, agent_id: str) -> bool:
        """Remove an agent registration.
        Discovered agents cannot be unregistered via the API.

        Args:
            agent_id: The agent's unique identifier.

        Returns:
            ``True`` if removed, ``False`` if not found or protected.
        """
        if agent_id in self._discovered:
            return False
        if agent_id not in self._agents:
            return False
        del self._agents[agent_id]
        self._heartbeats.pop(agent_id, None)
        self._save()
        return True

    def purge_stale(self) -> int:
        """Remove agents that haven't sent a heartbeat in
        ``HEARTBEAT_PURGE`` seconds.

        Returns:
            Number of agents removed.
        """
        now = time.time()
        stale = [
            aid
            for aid, ts in self._heartbeats.items()
            if now - ts > HEARTBEAT_PURGE
        ]
        for aid in stale:
            self._agents.pop(aid, None)
            self._heartbeats.pop(aid, None)
        if stale:
            logger.info("Purged %d stale agents: %s", len(stale), stale)
            self._save()
        return len(stale)

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    def stats(self) -> Dict[str, Any]:
        """Return registry statistics.

        Returns:
            Dict with keys: ``totalAgents``, ``aliveAgents``, ``staleAgents``,
            ``discoveredAgents``, ``externalAgents``.
        """
        now = time.time()
        alive = sum(
            1
            for aid in self._agents
            if aid not in self._heartbeats
            or now - self._heartbeats.get(aid, 0) <= HEARTBEAT_TIMEOUT
        )
        return {
            "totalAgents": len(self._agents) + len(self._discovered),
            "aliveAgents": alive + len(self._discovered),
            "staleAgents": len(self._agents) - alive,
            "discoveredAgents": len(self._discovered),
            "externalAgents": len(self._agents),
        }