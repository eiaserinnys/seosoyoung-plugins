"""Memory 플러그인

Observational Memory 기반의 관찰/기억 시스템을 제공합니다.
"""

from seosoyoung_plugins.memory.observer import Observer, ObserverResult, parse_observer_output
from seosoyoung_plugins.memory.reflector import Reflector, ReflectorResult
from seosoyoung_plugins.memory.promoter import Promoter, PromoterResult, Compactor, CompactorResult
from seosoyoung_plugins.memory.store import (
    MemoryStore,
    MemoryRecord,
    ObservationItem,
    PersistentItem,
    generate_obs_id,
    generate_ltm_id,
)
from seosoyoung_plugins.memory.observation_pipeline import (
    observe_conversation,
    render_observation_items,
    render_persistent_items,
)
from seosoyoung_plugins.memory.migration import migrate_memory_dir, MigrationReport

__all__ = [
    # Observer
    "Observer",
    "ObserverResult",
    "parse_observer_output",
    # Reflector
    "Reflector",
    "ReflectorResult",
    # Promoter/Compactor
    "Promoter",
    "PromoterResult",
    "Compactor",
    "CompactorResult",
    # Store
    "MemoryStore",
    "MemoryRecord",
    "ObservationItem",
    "PersistentItem",
    "generate_obs_id",
    "generate_ltm_id",
    # Pipeline
    "observe_conversation",
    "render_observation_items",
    "render_persistent_items",
    # Migration
    "migrate_memory_dir",
    "MigrationReport",
]
