"""
osprey.ui.workers
~~~~~~~~~~~~~~~~~
QThread workers for running search and replace operations
without blocking the UI event loop.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QThread, Signal

from osprey.engine.base import SearchEngine, SearchQuery, SearchResult

logger = logging.getLogger(__name__)


class SearchWorker(QThread):
    """Runs a search in a background thread and emits result_ready when done."""

    result_ready = Signal(SearchResult)
    error = Signal(str)

    def __init__(self, engine: SearchEngine, query: SearchQuery, parent=None) -> None:
        super().__init__(parent)
        self._engine = engine
        self._query = query

    def run(self) -> None:
        try:
            result = self._engine.search(self._query)
            if result.error:
                logger.warning("[Worker] Search returned error: %s", result.error)
                self.error.emit(result.error)
            else:
                filtered = self._query.file_filter.filter_result(result)
                if filtered is not result:
                    logger.debug(
                        "[Worker] File filter reduced result: %d -> %d files",
                        result.total_file_count,
                        filtered.total_file_count,
                    )
                result = filtered
                self.result_ready.emit(result)
        except Exception as exc:
            logger.exception("[Worker] Unhandled exception during search")
            self.error.emit(str(exc))
