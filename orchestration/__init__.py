"""Sprint 3 — orchestration foundation.

Public re-exports. Nothing in this package runs autonomously; the
orchestrator is a library that Sprint 4 will wire into FastAPI's lifespan.
"""

from orchestration.event_envelope import EventEnvelope, new_envelope
from orchestration.retry import RetryPolicy, RetryExhausted, with_retry
from orchestration.circuit_breaker import (
    CircuitBreaker,
    CircuitOpenError,
    CircuitRegistry,
    CircuitState,
    default_registry,
)
from orchestration.critic import (
    BaseCritic,
    ChainCritic,
    CritiqueResult,
    SchemaCritic,
)
from orchestration.base_agent import (
    BaseAgent,
    StreamAgent,
    TickAgent,
)
from orchestration.event_bus import (
    EventBus,
    InMemoryEventBus,
    RedisEventBus,
    stream_name,
    dlq_stream_name,
)
from orchestration.orchestrator import (
    Orchestrator,
    AgentHealth,
    AgentStatus,
)

__all__ = [
    # Envelope
    "EventEnvelope", "new_envelope",
    # Retry
    "RetryPolicy", "RetryExhausted", "with_retry",
    # Circuit breaker
    "CircuitBreaker", "CircuitOpenError", "CircuitRegistry",
    "CircuitState", "default_registry",
    # Critic
    "BaseCritic", "ChainCritic", "CritiqueResult", "SchemaCritic",
    # Agents
    "BaseAgent", "StreamAgent", "TickAgent",
    # Event bus
    "EventBus", "InMemoryEventBus", "RedisEventBus",
    "stream_name", "dlq_stream_name",
    # Orchestrator
    "Orchestrator", "AgentHealth", "AgentStatus",
]
