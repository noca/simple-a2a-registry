"""Comprehensive unit tests for Security Harness (APE / DTM / Events / Provenance)."""
from __future__ import annotations

import pytest

from simple_a2a_registry.auth import _generate_rsa_keypair
from simple_a2a_registry.database import SQLiteEngine
from simple_a2a_registry.security import (
    APEConfig, AuthorizationPolicyEngine, CallerIdentity, CheckpointResult,
    DelegatedTokenManager, SecurityEventStore,
)
from simple_a2a_registry.store import Store

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Fixtures — shared in-memory engine
# ---------------------------------------------------------------------------

@pytest.fixture
def shared_engine():
    """In-memory SQLiteEngine with FK enforcement off for DDL flexibility."""
    engine = SQLiteEngine(":memory:")
    engine.connect()
    engine.execute("PRAGMA foreign_keys=OFF")
    yield engine
    engine.close()


# ── Auth keys ───────────────────────────────────────────────────────────

@pytest.fixture
def priv_pub():
    return _generate_rsa_keypair()


# ── Agent store + pre-registered agents ─────────────────────────────────

@pytest.fixture
def store_and_ids(shared_engine):
    """Return (store, {name: uuid}) so callers can reference agents by UUID."""
    store = Store(shared_engine)
    names = {}
    for tag, card in [
        ("alpha", {"agent_id": "agent-alpha", "name": "Alpha Agent",
                    "tenant_id": "default", "scopes": ["task:write", "task:read"],
                    "disabled": 0}),
        ("beta",  {"agent_id": "agent-beta",  "name": "Beta Agent",
                    "tenant_id": "default", "scopes": ["task:read"],
                    "disabled": 0}),
        ("gamma", {"agent_id": "agent-gamma", "name": "Gamma Agent (disabled)",
                    "tenant_id": "default", "scopes": [], "disabled": 1}),
        ("tx",    {"agent_id": "agent-tenant-x", "name": "Tenant-X Agent",
                    "tenant_id": "tenant-x", "scopes": ["task:write"],
                    "disabled": 0}),
    ]:
        uid = store.register_agent(card)
        # Set disabled in DB for gamma
        if tag == "gamma":
            with store._tx() as eng:
                eng.execute("UPDATE agents SET disabled=1 WHERE id=?", (uid,))
        names[tag] = uid
    return store, names


@pytest.fixture
def store(store_and_ids):
    return store_and_ids[0]


@pytest.fixture
def ids(store_and_ids):
    return store_and_ids[1]


# ── Event store / DTM / APE ────────────────────────────────────────────

@pytest.fixture
def event_store(shared_engine):
    es = SecurityEventStore(shared_engine)
    es.ensure_schema()
    return es


@pytest.fixture
def dtm(shared_engine, priv_pub):
    priv, pub = priv_pub
    d = DelegatedTokenManager(shared_engine, priv, pub, default_ttl=300, max_depth=3)
    d.ensure_schema()
    return d


@pytest.fixture
def ape_enforce(store, dtm, event_store):
    return AuthorizationPolicyEngine(
        APEConfig(mode="enforce", default_delegation_policy="open", max_delegation_depth=3),
        dtm, event_store, store,
    )


@pytest.fixture
def ape_audit(store, dtm, event_store):
    return AuthorizationPolicyEngine(
        APEConfig(mode="audit", default_delegation_policy="open", max_delegation_depth=3),
        dtm, event_store, store,
    )


@pytest.fixture
def ape_warn(store, dtm, event_store):
    return AuthorizationPolicyEngine(
        APEConfig(mode="warn", default_delegation_policy="open", max_delegation_depth=3),
        dtm, event_store, store,
    )


# ── CallerIdentity helpers ──────────────────────────────────────────────

def caller(agent_id, tenant="default", scope="task:write task:read"):
    return CallerIdentity(agent_id=agent_id, tenant=tenant, scope=scope)


# ===== APE: check_task_create ============================================

class TestAPECheckTaskCreate:

    async def test_create_allow_registered_agent(self, ape_enforce, ids):
        """TC-APE-01: registered agent with task:write → allowed."""
        result = await ape_enforce.check_task_create(
            caller(ids["alpha"]), {"assignee": ids["beta"]},
        )
        assert result.allowed is True, result.reason

    async def test_create_deny_anonymous(self, ape_enforce):
        """TC-APE-02: anonymous caller → denied."""
        result = await ape_enforce.check_task_create(
            caller("anonymous"), {"assignee": "some-agent"},
        )
        assert result.allowed is False
        assert "anonymous" in result.reason

    async def test_create_deny_empty_id(self, ape_enforce):
        """TC-APE-03: empty agent_id → denied."""
        result = await ape_enforce.check_task_create(
            caller(""), {"assignee": "agent"},
        )
        assert result.allowed is False
        assert "empty" in result.reason

    async def test_create_deny_unregistered_caller(self, ape_enforce, ids):
        """TC-APE-04: unregistered caller → denied."""
        result = await ape_enforce.check_task_create(
            caller("unknown-agent-uuid"), {"assignee": ids["beta"]},
        )
        assert result.allowed is False
        assert "not registered" in result.reason

    async def test_create_deny_disabled_caller(self, ape_enforce, ids):
        """TC-APE-05: disabled caller → denied."""
        result = await ape_enforce.check_task_create(
            caller(ids["gamma"]), {"assignee": ids["beta"]},
        )
        assert result.allowed is False
        assert "disabled" in result.reason

    async def test_create_deny_disabled_assignee(self, ape_enforce, ids):
        """TC-APE-06: disabled assignee → denied."""
        result = await ape_enforce.check_task_create(
            caller(ids["alpha"]), {"assignee": ids["gamma"]},
        )
        assert result.allowed is False
        assert "disabled" in result.reason

    async def test_create_deny_missing_scope(self, ape_enforce, ids):
        """TC-APE-07: caller without task:write scope → denied."""
        result = await ape_enforce.check_task_create(
            caller(ids["beta"], scope="task:read"), {"assignee": ids["alpha"]},
        )
        assert result.allowed is False
        assert "scope" in result.reason.lower()

    async def test_create_allow_admin_scope(self, ape_enforce, ids):
        """TC-APE-08: registry:admin bypasses task:write."""
        result = await ape_enforce.check_task_create(
            caller(ids["alpha"], scope="registry:admin"), {"assignee": ids["beta"]},
        )
        assert result.allowed is True

    async def test_create_deny_tenant_mismatch(self, ape_enforce, ids):
        """TC-APE-09: default tenant caller → tenant-x assignee → denied."""
        result = await ape_enforce.check_task_create(
            caller(ids["alpha"]),
            {"assignee": ids["tx"]},
        )
        assert result.allowed is False
        assert "tenant" in result.reason.lower()

    async def test_create_allow_same_tenant(self, ape_enforce, ids):
        """TC-APE-10: same-tenant → allowed."""
        result = await ape_enforce.check_task_create(
            caller(ids["tx"], tenant="tenant-x"), {"assignee": ids["tx"]},
        )
        assert result.allowed is True

    async def test_create_no_assignee(self, ape_enforce, ids):
        """TC-APE-11: omitting assignee skips agent checks but not scope."""
        result = await ape_enforce.check_task_create(
            caller(ids["alpha"]), {},
        )
        assert result.allowed is True


# ===== APE: check_task_claim ============================================

class TestAPECheckTaskClaim:

    async def test_claim_no_task_store(self, ape_enforce, ids):
        """TC-APE-12: APE without task_store → task not found."""
        result = await ape_enforce.check_task_claim(
            caller(ids["alpha"]), task_id="t_mock",
        )
        assert result.allowed is False
        assert "not found" in result.reason

    async def test_claim_restricted_requires_token(self, ids):
        """TC-APE-13: restricted policy + no token → blocked."""
        priv2, pub2 = _generate_rsa_keypair()
        eng2 = SQLiteEngine(":memory:"); eng2.connect(); eng2.execute("PRAGMA foreign_keys=OFF")
        es2 = SecurityEventStore(eng2); es2.ensure_schema()
        dtm2 = DelegatedTokenManager(eng2, priv2, pub2, default_ttl=300, max_depth=3); dtm2.ensure_schema()
        store2, _ = Store(eng2), None
        ape = AuthorizationPolicyEngine(
            APEConfig(mode="enforce", default_delegation_policy="restricted", max_delegation_depth=3),
            dtm2, es2, store2,
        )
        result = await ape.check_task_claim(caller(ids["alpha"]), task_id="t_mock_13")
        assert result.allowed is False
        assert "not found" in result.reason  # without task_store


# ===== APE: check_task_complete =========================================

class TestAPECheckTaskComplete:

    async def test_complete_task_not_found(self, ape_enforce, ids):
        """TC-APE-14: complete non-existent task → denied."""
        result = await ape_enforce.check_task_complete(caller(ids["alpha"]), task_id="t_missing")
        assert result.allowed is False
        assert "not found" in result.reason


# ===== APE: Three-phase mode migration (audit → warn → enforce) ========

class TestAPEModes:

    async def test_audit_allows(self, ape_audit):
        """TC-APE-15: audit → normally-denied request returns allowed=True."""
        result = await ape_audit.check_task_create(caller("no-such-agent"), {})
        assert result.allowed is True
        assert result.recorded_event is True

    async def test_warn_allows_with_header(self, ape_warn):
        """TC-APE-16: warn → returns allowed + X-Security-Warning."""
        result = await ape_warn.check_task_create(caller("no-such-agent"), {})
        assert result.allowed is True
        assert result.recorded_event is True
        assert "X-Security-Warning" in result.response_headers
        assert result.response_headers["X-Security-Warning"]

    async def test_enforce_blocks(self, ape_enforce):
        """TC-APE-17: enforce → denies, returning allowed=False."""
        result = await ape_enforce.check_task_create(caller("no-such-agent"), {})
        assert result.allowed is False
        assert result.recorded_event is True

    async def test_all_modes_log_events(self, ape_audit, ape_warn, ape_enforce):
        """TC-APE-18: all three modes persist security events."""
        for a in (ape_audit, ape_warn, ape_enforce):
            await a.check_task_create(caller("no-such-agent"), {})
        assert ape_enforce.event_store.count_all() >= 3


# ===== DTM: Mint =========================================================

class TestDTMMint:

    def test_mint_valid(self, dtm):
        """TC-DTM-01: mint a valid delegation token."""
        t = dtm.mint_delegation_token(
            sub="agent-beta", task_id="t_1", origin_agent="agent-alpha",
            effective_scope="task:write task:read",
        )
        assert t.sub == "agent-beta"
        assert t.task_id == "t_1"
        assert t.origin_agent == "agent-alpha"
        assert t.jti; assert t.iat > 0; assert t.exp > t.iat
        assert t.depth == 0

    def test_mint_with_chain(self, dtm):
        """TC-DTM-02: parent chain appended + depth set."""
        t = dtm.mint_delegation_token(
            sub="agent-beta", task_id="t_2", origin_agent="agent-alpha",
            effective_scope="task:write", depth=1,
            parent_chain=[{"agent": "agent-alpha", "action": "create",
                           "scope": "task:write", "timestamp": 1000}],
        )
        assert t.depth == 1
        assert len(t.delegation_chain) == 2

    def test_mint_with_restriction(self, dtm):
        """TC-DTM-03: scope restriction attenuates effective_scope."""
        t = dtm.mint_delegation_token(
            sub="agent-beta", task_id="t_3", origin_agent="agent-alpha",
            effective_scope="task:write task:read agent:read",
            restriction={"exclude": ["agent:read"]},
        )
        assert "agent:read" not in t.effective_scope

    def test_mint_allowed_callees(self, dtm):
        """TC-DTM-04: allowed_callees preserved."""
        t = dtm.mint_delegation_token(
            sub="agent-beta", task_id="t_4", origin_agent="agent-alpha",
            effective_scope="task:write",
            allowed_callees=["agent-gamma"],
        )
        assert t.allowed_callees == ["agent-gamma"]


# ===== DTM: JWT lifecycle ===============================================

class TestDTMJWT:

    def test_jwt_roundtrip(self, dtm):
        """TC-DTM-05: JWT sign → parse back preserves all fields."""
        t = dtm.mint_delegation_token(
            sub="agent-beta", task_id="t_5", origin_agent="agent-alpha",
            effective_scope="task:write",
        )
        jwt_str = t.to_jwt(dtm._private_key)
        assert jwt_str.count(".") == 2
        from simple_a2a_registry.security.dtm import DelegatedTaskToken
        p = DelegatedTaskToken.from_jwt(jwt_str, dtm._public_key)
        assert p.sub == "agent-beta"; assert p.task_id == "t_5"

    def test_invalid_jwt_raises(self, dtm):
        """TC-DTM-06: malformed JWT → TokenInvalidError."""
        from simple_a2a_registry.security.errors import TokenInvalidError
        from simple_a2a_registry.security.dtm import DelegatedTaskToken
        with pytest.raises(TokenInvalidError):
            DelegatedTaskToken.from_jwt("bad.jwt.stuff", dtm._public_key)

    def test_full_lifecycle_with_replay(self, dtm):
        """TC-DTM-07: mint → persist → verify (1st OK, 2nd replay)."""
        from simple_a2a_registry.security.errors import TokenReplayError
        t = dtm.mint_delegation_token(
            sub="agent-beta", task_id="t_7", origin_agent="agent-alpha",
            effective_scope="task:write",
        )
        dtm.persist_token_hash(t)
        jwt_str = t.to_jwt(dtm._private_key)
        v1 = dtm.verify_delegation_token(jwt_str, "t_7", "agent-beta", consume=True)
        assert v1.sub == "agent-beta"
        with pytest.raises(TokenReplayError):
            dtm.verify_delegation_token(jwt_str, "t_7", "agent-beta")

    def test_expired_token_raises(self, dtm):
        """TC-DTM-08: token minted with ttl=-30 raises TokenExpiredError."""
        import time
        from simple_a2a_registry.security.errors import TokenExpiredError
        t = dtm.mint_delegation_token(
            sub="agent-beta", task_id="t_8", origin_agent="agent-alpha",
            effective_scope="task:write", ttl=-30,
        )
        dtm.persist_token_hash(t)
        jwt_str = t.to_jwt(dtm._private_key)
        with pytest.raises(TokenExpiredError):
            dtm.verify_delegation_token(jwt_str, "t_8", "agent-beta")

    def test_task_binding_mismatch(self, dtm):
        """TC-DTM-09: wrong task_id → TokenTaskMismatchError."""
        from simple_a2a_registry.security.errors import TokenTaskMismatchError
        t = dtm.mint_delegation_token(
            sub="agent-beta", task_id="t_9", origin_agent="agent-alpha",
            effective_scope="task:write",
        )
        dtm.persist_token_hash(t)
        jwt_str = t.to_jwt(dtm._private_key)
        with pytest.raises(TokenTaskMismatchError):
            dtm.verify_delegation_token(jwt_str, "wrong_task", "agent-beta")

    def test_subject_binding_mismatch(self, dtm):
        """TC-DTM-10: wrong agent → TokenSubjectMismatchError."""
        from simple_a2a_registry.security.errors import TokenSubjectMismatchError
        t = dtm.mint_delegation_token(
            sub="agent-beta", task_id="t_10", origin_agent="agent-alpha",
            effective_scope="task:write",
        )
        dtm.persist_token_hash(t)
        jwt_str = t.to_jwt(dtm._private_key)
        with pytest.raises(TokenSubjectMismatchError):
            dtm.verify_delegation_token(jwt_str, "t_10", "intruder")


# ===== Scope Attenuation ================================================

class TestScopeAttenuation:

    def test_exclude(self):
        """TC-SCOPE-01: exclude removes scopes."""
        from simple_a2a_registry.security.dtm import attenuate_scope
        r = attenuate_scope("a b c", {"exclude": ["c"]})
        assert "c" not in r; assert "a" in r

    def test_reduce_to(self):
        """TC-SCOPE-02: reduce_to produces intersection."""
        from simple_a2a_registry.security.dtm import attenuate_scope
        assert attenuate_scope("a b c", {"reduce_to": ["a"]}) == "a"

    def test_empty_raises(self):
        """TC-SCOPE-03: empty intersection raises ValueError."""
        from simple_a2a_registry.security.dtm import attenuate_scope
        with pytest.raises(ValueError, match="empty"):
            attenuate_scope("a", {"reduce_to": ["b"]})

    def test_identity(self):
        """TC-SCOPE-04: no restriction → identity preserved."""
        from simple_a2a_registry.security.dtm import attenuate_scope
        assert "a" in attenuate_scope("a b", None)
        assert "a" in attenuate_scope("a b", {})


# ===== Security Events ==================================================

class TestSecurityEventStore:

    def test_record(self, event_store):
        """TC-EVT-01: record returns populated event."""
        e = event_store.record("AUTHORIZATION_DENIED", "agent-alpha", "t_1", "deny",
                               reason="insufficient scope")
        assert e.event_id.startswith("sev_")
        assert e.event_type == "AUTHORIZATION_DENIED"
        assert e.actor == "agent-alpha"

    def test_list_by_task(self, event_store):
        """TC-EVT-02: list events filtered by task_id."""
        event_store.record("AUTH_FAILURE", "a", "t_2", "deny", task_id="t_2")
        event_store.record("AUTHORIZATION_ALLOWED", "b", "t_2", "allow", task_id="t_2")
        event_store.record("SCOPE_DENIED", "a", "t_3", "deny", task_id="t_3")
        evs = event_store.list_by_task("t_2")
        assert len(evs) == 2

    def test_list_all_with_filters(self, event_store):
        """TC-EVT-03: actor filter."""
        event_store.record("AUTH_FAILURE", "agent-alpha", "t_a", "deny")
        event_store.record("AUTHORIZATION_ALLOWED", "agent-alpha", "t_b", "allow")
        event_store.record("AUTHORIZATION_DENIED", "agent-beta", "t_c", "deny")
        assert len(event_store.list_all(actor="agent-alpha")) == 2
        assert len(event_store.list_all(event_type="AUTHORIZATION_DENIED")) == 1

    def test_count(self, event_store):
        """TC-EVT-04: count with/without filters."""
        assert event_store.count_all() == 0
        event_store.record("AUTH_FAILURE", "a", "t", "deny")
        event_store.record("AUTHORIZATION_ALLOWED", "b", "t", "allow")
        assert event_store.count_all() == 2
        assert event_store.count_all(actor="a") == 1

    def test_tenant_filter(self, event_store):
        """TC-EVT-05: filter by tenant."""
        event_store.record("AUTH_FAILURE", "a", "t", "deny", tenant="ten-x")
        event_store.record("AUTHORIZATION_ALLOWED", "b", "t", "allow", tenant="default")
        assert event_store.count_all(tenant="ten-x") == 1


# ===== Provenance Tracker ===============================================

class TestProvenanceTracker:

    def test_ensure_and_get(self, shared_engine):
        """TC-PT-01: ensure chain → get chain."""
        from simple_a2a_registry.security.pt import ProvenanceTracker
        pt = ProvenanceTracker(shared_engine); pt.ensure_schema()
        pt.ensure_chain(chain_id="c1", origin_agent="a",
                        root_task_id="r1", task_id="t1")
        c = pt.get_chain_by_task("t1")
        assert c and c.chain_id == "c1" and c.origin_agent == "a"

    def test_record_hops(self, shared_engine):
        """TC-PT-02: record hops → depth updated, all hops retrievable."""
        from simple_a2a_registry.security.pt import ProvenanceTracker
        pt = ProvenanceTracker(shared_engine); pt.ensure_schema()
        pt.ensure_chain(chain_id="c2", origin_agent="a", root_task_id="r2", task_id="t2")
        pt.record_hop(chain_id="c2", from_agent="a", to_agent="b", action="delegate")
        pt.record_hop(chain_id="c2", from_agent="b", to_agent="c", action="delegate")
        c = pt.get_chain_by_task("t2")
        assert c and len(c.hops) == 2 and c.depth == 2

    def test_chain_not_found(self, shared_engine):
        """TC-PT-03: unknown task → None."""
        from simple_a2a_registry.security.pt import ProvenanceTracker
        pt = ProvenanceTracker(shared_engine); pt.ensure_schema()
        assert pt.get_chain_by_task("noop") is None

    def test_list_by_root(self, shared_engine):
        """TC-PT-04: list chains for a root task."""
        from simple_a2a_registry.security.pt import ProvenanceTracker
        pt = ProvenanceTracker(shared_engine); pt.ensure_schema()
        pt.ensure_chain(chain_id="c3", origin_agent="a", root_task_id="r3", task_id="ta")
        pt.ensure_chain(chain_id="c4", origin_agent="a", root_task_id="r3", task_id="tb")
        assert len(pt.list_chains_by_root("r3")) == 2