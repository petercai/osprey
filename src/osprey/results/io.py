"""
osprey.results.io
~~~~~~~~~~~~~~~~~
Convert between SearchResult / ResultSnapshot and persist to / restore from .opr files.

.opr is a JSON file format that captures both search query metadata and all
matched file+line content so the user can reload results without re-running
the search engine.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from osprey.engine.base import FileMatches, Match, FileFilter, SearchQuery, SearchResult
from osprey.results.model import ResultSnapshot, ResultFileEntry, ResultMatch

logger = logging.getLogger(__name__)


def from_search_result(result: SearchResult) -> ResultSnapshot:
    """Convert a live SearchResult to a ResultSnapshot for serialisation."""
    q = result.query
    ff = q.file_filter

    file_entries = [
        ResultFileEntry(
            file_path=str(fm.file_path),
            matches=[
                ResultMatch(
                    line_number=m.line_number,
                    column_start=m.column_start,
                    column_end=m.column_end,
                    line_text=m.line_text,
                    match_text=m.match_text,
                )
                for m in fm.matches
            ],
        )
        for fm in result.files
    ]

    return ResultSnapshot(
        saved_at=datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
        pattern=q.pattern,
        paths=[str(p) for p in q.paths],
        regex=q.regex,
        case_sensitive=q.case_sensitive,
        whole_word=q.whole_word,
        include_rules=list(ff.include_rules),
        exclude_rules=list(ff.exclude_rules),
        use_regex_rules=ff.use_regex,
        engine_used=result.engine_used,
        elapsed_ms=result.elapsed_ms,
        total_files=result.total_file_count,
        total_matches=result.total_match_count,
        files=file_entries,
    )


def to_search_result(snapshot: ResultSnapshot) -> SearchResult:
    """Reconstruct a displayable SearchResult from a loaded ResultSnapshot.

    The reconstructed result carries a "[loaded]" prefix in engine_used to
    signal that it comes from disk rather than a live search run.
    """
    file_filter = FileFilter(
        include_rules=list(snapshot.include_rules),
        exclude_rules=list(snapshot.exclude_rules),
        use_regex=snapshot.use_regex_rules,
    )
    query = SearchQuery(
        pattern=snapshot.pattern,
        paths=[Path(p) for p in snapshot.paths] if snapshot.paths else [Path(".")],
        file_filter=file_filter,
        regex=snapshot.regex,
        case_sensitive=snapshot.case_sensitive,
        whole_word=snapshot.whole_word,
    )
    files = [
        FileMatches(
            file_path=Path(fe.file_path),
            encoding="utf-8",
            matches=[
                Match(
                    line_number=m.line_number,
                    column_start=m.column_start,
                    column_end=m.column_end,
                    line_text=m.line_text,
                    match_text=m.match_text,
                )
                for m in fe.matches
            ],
        )
        for fe in snapshot.files
    ]
    return SearchResult(
        query=query,
        files=files,
        elapsed_ms=snapshot.elapsed_ms,
        engine_used=f"[loaded] {snapshot.engine_used}" if snapshot.engine_used else "[loaded]",
    )


def save(result: SearchResult, path: Path) -> ResultSnapshot:
    """Serialise a SearchResult to *path* (.opr file).

    Creates parent directories as needed. Returns the ResultSnapshot object written.
    """
    snapshot = from_search_result(result)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(snapshot.to_dict(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    logger.info(
        "[results.io] Saved %d files / %d matches to %s",
        snapshot.total_files, snapshot.total_matches, path,
    )
    return snapshot


def load(path: Path) -> ResultSnapshot:
    """Load a .opr file and return the ResultSnapshot dataclass."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    snapshot = ResultSnapshot.from_dict(raw)
    logger.info(
        "[results.io] Loaded %d files / %d matches from %s",
        snapshot.total_files, snapshot.total_matches, path,
    )
    return snapshot
