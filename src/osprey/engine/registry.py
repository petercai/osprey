"""
osprey.engine.registry
~~~~~~~~~~~~~~~~~~~~~~
Auto-detects available search backends and selects the best one.
Falls back gracefully: rg -> grep.
"""

from __future__ import annotations

import logging

from osprey.engine.base import SearchEngine
from osprey.engine.grep import GrepEngine
from osprey.engine.ripgrep import RipgrepEngine

logger = logging.getLogger(__name__)

# Priority order: fastest/feature-rich first
_ENGINE_CLASSES = [RipgrepEngine, GrepEngine]


class EngineRegistry:
    """Manages available search engines and selects the best one."""

    def __init__(self) -> None:
        self._engines: list[SearchEngine] = []
        self._detect()

    def _detect(self) -> None:
        for cls in _ENGINE_CLASSES:
            engine: SearchEngine = cls()  # type: ignore[call-arg]
            if engine.is_available():
                ver = engine.version()
                logger.info("[Engine] %s detected — %s", engine.name(), ver)
                self._engines.append(engine)
            else:
                logger.debug("[Engine] %s not found, skipping", engine.name())

        if not self._engines:
            logger.error("[Engine] No search engines available!")

    def available_names(self) -> list[str]:
        """Return names of all detected engines."""
        return [e.name() for e in self._engines]

    def best_engine(self) -> SearchEngine:
        """Return highest-priority available engine."""
        if not self._engines:
            raise RuntimeError("No search engine available. Install rg or grep.")
        return self._engines[0]

    def get_engine(self, name: str) -> SearchEngine | None:
        """Return a specific engine by name, or None if not available."""
        for e in self._engines:
            if e.name() == name:
                return e
        return None
