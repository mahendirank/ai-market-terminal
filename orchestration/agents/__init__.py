"""orchestration.agents — concrete agent implementations.

Each agent wraps an existing legacy code path or implements a new
capability behind a per-agent feature flag.

Sprint 4 ships:
  - NewsFetchAgent (Stage 4.3 — shadow / dual-run for news fetching)
  - SignalCriticAgent (Stage 4.4 — observe-only critic; no producer yet)
"""

from orchestration.agents.news_fetch_agent import NewsFetchAgent
from orchestration.agents.signal_critic_agent import SignalCriticAgent

__all__ = ["NewsFetchAgent", "SignalCriticAgent"]
