"""
osprey.results.model
~~~~~~~~~~~~~~~~~~~~
Dataclass model for .opr (Osprey Query Results) snapshot files.

File format (JSON):
{
  "version": "1.0",
  "saved_at": "2026-06-24T10:00:00+00:00",
  "query": { ... },
  "meta":  { engine_used, elapsed_ms, total_files, total_matches },
  "files": [ { "file_path": "...", "matches": [...] }, ... ]
}
"""
from __future__ import annotations

import dataclasses

SNAPSHOT_VERSION = "1.0"


@dataclasses.dataclass
class ResultMatch:
    """Serialisable representation of a single line match."""

    line_number: int
    column_start: int
    column_end: int
    line_text: str
    match_text: str

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ResultMatch":
        return cls(
            line_number=int(d.get("line_number", 0)),
            column_start=int(d.get("column_start", 0)),
            column_end=int(d.get("column_end", 0)),
            line_text=str(d.get("line_text", "")),
            match_text=str(d.get("match_text", "")),
        )


@dataclasses.dataclass
class ResultFileEntry:
    """Serialisable representation of all matches within one file."""

    file_path: str  # stored as absolute path string
    matches: list[ResultMatch] = dataclasses.field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "file_path": self.file_path,
            "matches": [m.to_dict() for m in self.matches],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ResultFileEntry":
        return cls(
            file_path=str(d.get("file_path", "")),
            matches=[ResultMatch.from_dict(m) for m in d.get("matches", [])],
        )


@dataclasses.dataclass
class ResultSnapshot:
    """Complete serialisable snapshot of a saved search result set.

    A .opr file stores both the query parameters (so the user can re-run
    the same search) and the matched content (so the user can browse results
    offline without re-running the engine).
    """

    version: str = SNAPSHOT_VERSION
    saved_at: str = ""              # ISO-8601 UTC timestamp

    # Search query metadata — mirrors SearchQuery / SearchProfile
    pattern: str = ""
    paths: list[str] = dataclasses.field(default_factory=list)
    regex: bool = False
    case_sensitive: bool = True
    whole_word: bool = False
    include_rules: list[str] = dataclasses.field(default_factory=list)
    exclude_rules: list[str] = dataclasses.field(default_factory=list)
    use_regex_rules: bool = False

    # Engine metadata
    engine_used: str = ""
    elapsed_ms: float = 0.0

    # Aggregate statistics (pre-computed for quick display without scanning files[])
    total_files: int = 0
    total_matches: int = 0

    # Per-file match data
    files: list[ResultFileEntry] = dataclasses.field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "saved_at": self.saved_at,
            "query": {
                "pattern": self.pattern,
                "paths": self.paths,
                "regex": self.regex,
                "case_sensitive": self.case_sensitive,
                "whole_word": self.whole_word,
                "include_rules": self.include_rules,
                "exclude_rules": self.exclude_rules,
                "use_regex_rules": self.use_regex_rules,
            },
            "meta": {
                "engine_used": self.engine_used,
                "elapsed_ms": self.elapsed_ms,
                "total_files": self.total_files,
                "total_matches": self.total_matches,
            },
            "files": [f.to_dict() for f in self.files],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ResultSnapshot":
        query = d.get("query", {})
        meta = d.get("meta", {})
        return cls(
            version=str(d.get("version", SNAPSHOT_VERSION)),
            saved_at=str(d.get("saved_at", "")),
            pattern=str(query.get("pattern", "")),
            paths=[str(p) for p in query.get("paths", [])],
            regex=bool(query.get("regex", False)),
            case_sensitive=bool(query.get("case_sensitive", True)),
            whole_word=bool(query.get("whole_word", False)),
            include_rules=[str(r) for r in query.get("include_rules", [])],
            exclude_rules=[str(r) for r in query.get("exclude_rules", [])],
            use_regex_rules=bool(query.get("use_regex_rules", False)),
            engine_used=str(meta.get("engine_used", "")),
            elapsed_ms=float(meta.get("elapsed_ms", 0.0)),
            total_files=int(meta.get("total_files", 0)),
            total_matches=int(meta.get("total_matches", 0)),
            files=[ResultFileEntry.from_dict(f) for f in d.get("files", [])],
        )
