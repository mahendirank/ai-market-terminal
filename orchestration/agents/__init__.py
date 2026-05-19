"""orchestration.agents — concrete agent implementations.

Each agent wraps an existing legacy code path or implements a new
capability behind a per-agent feature flag. Sprint 4 ships one:
NewsFetchAgent (shadow / dual-run mode).
"""

from orchestration.agents.news_fetch_agent import NewsFetchAgent

__all__ = ["NewsFetchAgent"]
