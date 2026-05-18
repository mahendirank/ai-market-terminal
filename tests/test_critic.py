"""Sprint 3 — Critic tests."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from orchestration.critic import (
    AlwaysAcceptCritic,
    BaseCritic,
    ChainCritic,
    CritiqueResult,
    SchemaCritic,
)
from orchestration.event_envelope import new_envelope


@pytest.mark.smoke
def test_critique_result_validates_confidence():
    with pytest.raises(ValueError):
        CritiqueResult(accepted=True, reason="ok", confidence=1.5)


@pytest.mark.smoke
def test_critique_result_factories():
    a = CritiqueResult.accept()
    assert a.accepted and a.reason == "ok"
    r = CritiqueResult.reject("bad", markers={"hallucination"}, detail="d")
    assert not r.accepted and "hallucination" in r.markers


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_always_accept_critic():
    env = new_envelope(event_type="x", payload={}, agent_name="a")
    result = await AlwaysAcceptCritic().evaluate(env)
    assert result.accepted


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_schema_critic_accept():
    def pred(payload):
        return payload.get("ok") is True, None
    critic = SchemaCritic(name="t", predicate=pred)
    env = new_envelope(event_type="x", payload={"ok": True}, agent_name="a")
    assert (await critic.evaluate(env)).accepted


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_schema_critic_reject():
    def pred(payload):
        return False, "missing_field_x"
    critic = SchemaCritic(name="t", predicate=pred)
    env = new_envelope(event_type="x", payload={}, agent_name="a")
    result = await critic.evaluate(env)
    assert not result.accepted
    assert result.reason == "missing_field_x"


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_schema_critic_predicate_exception_is_safe():
    def boom(payload):
        raise RuntimeError("predicate exploded")
    critic = SchemaCritic(name="t", predicate=boom)
    env = new_envelope(event_type="x", payload={}, agent_name="a")
    result = await critic.evaluate(env)
    assert not result.accepted
    assert result.reason == "critic_internal_error"
    assert result.confidence == 0.0


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_chain_critic_halts_on_first_reject():
    calls = []

    class CountedAccept(BaseCritic):
        def __init__(self, name): self.name = name
        async def evaluate(self, env):
            calls.append(self.name)
            return CritiqueResult.accept()

    class CountedReject(BaseCritic):
        def __init__(self, name): self.name = name
        async def evaluate(self, env):
            calls.append(self.name)
            return CritiqueResult.reject(f"{self.name}_said_no")

    chain = ChainCritic(name="c", critics=[
        CountedAccept("a"),
        CountedReject("b"),
        CountedAccept("c"),
    ])
    env = new_envelope(event_type="x", payload={}, agent_name="a")
    result = await chain.evaluate(env)
    assert not result.accepted
    assert result.reason == "b_said_no"
    assert calls == ["a", "b"]  # c never ran


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_chain_critic_unions_markers():
    class MarkedAccept(BaseCritic):
        def __init__(self, name, markers): self.name = name; self._markers = markers
        async def evaluate(self, env):
            return CritiqueResult(accepted=True, reason="ok", markers=frozenset(self._markers))

    chain = ChainCritic(name="c", critics=[
        MarkedAccept("a", {"future_date"}),
        MarkedAccept("b", {"impossible_price"}),
    ])
    env = new_envelope(event_type="x", payload={}, agent_name="a")
    result = await chain.evaluate(env)
    assert result.accepted
    assert result.markers == frozenset({"future_date", "impossible_price"})


@pytest.mark.smoke
def test_chain_critic_requires_nonempty():
    with pytest.raises(ValueError):
        ChainCritic(name="c", critics=[])
