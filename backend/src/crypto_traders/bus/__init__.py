"""Event bus interno: contrato unico, backends intercambiaveis."""

from __future__ import annotations

from ..config import Settings
from .base import EventBus, Topics
from .memory import InMemoryEventBus

__all__ = ["EventBus", "InMemoryEventBus", "Topics", "build_event_bus"]


def build_event_bus(settings: Settings) -> EventBus:
    """Escolhe o backend conforme `EVENT_BUS` no `.env`."""
    if settings.event_bus == "redis":
        from .redis_streams import RedisStreamsEventBus

        return RedisStreamsEventBus(settings.redis_url)
    return InMemoryEventBus()
