"""
osprey.engine.base
~~~~~~~~~~~~~~~~~~
Core data models and SearchEngine protocol for Osprey.
All search backends implement SearchEngine to keep the UI layer decoupled.
"""

from __future__ import annotations

import dataclasses
import fnmatch
import re
from pathlib import Path
from typing import Protocol, runtime_checkable


@dataclasses.dataclass
class FileFilter:
    """Criteria for which files are eligible for searching."""

    # Rules that a file path must match. Interpreted as glob or regex depending on use_regex.
    include_rules: list[str] = dataclasses.field(default_factory=list)
    # Rules that exclude a file path. Interpreted as glob or regex depending on use_regex.
    exclude_rules: list[str] = dataclasses.field(default_factory=list)
    # When True, include/exclude rules are treated as regexes instead of globs.
    use_regex: bool = False
    # Exclude well-known noise directories by default
    exclude_dirs: list[str] = dataclasses.field(
        default_factory=lambda: [
            ".git",
            "node_modules",
            "__pycache__",
            ".venv",
            "venv",
            "dist",
            "build",
            ".tox",
        ]
    )
    # Files larger than this are skipped (0 = no limit)
    max_size_bytes: int = 0
    @property
    def include_globs(self) -> list[str]:
        """Backward-compatible alias for older callers."""
        return self.include_rules

    @property
    def exclude_globs(self) -> list[str]:
        """Backward-compatible alias for older callers."""
        return self.exclude_rules

    def matches(self, path: Path) -> bool:
        """Return True when *path* satisfies all include/exclude rules."""
        path_text = path.as_posix()

        if self.include_rules and not any(self._rule_matches(rule, path_text) for rule in self.include_rules):
            return False

        if any(self._rule_matches(rule, path_text) for rule in self.exclude_rules):
            return False

        if any(part in self.exclude_dirs for part in path.parts):
            return False

        if self.max_size_bytes > 0:
            try:
                if path.is_file() and path.stat().st_size > self.max_size_bytes:
                    return False
            except OSError:
                return False

        return True

    def filter_result(self, result: "SearchResult") -> "SearchResult":
        """Return a SearchResult with file-level filters applied in memory."""
        filtered_files = [file_matches for file_matches in result.files if self.matches(file_matches.file_path)]
        if len(filtered_files) == len(result.files):
            return result
        return SearchResult(
            query=result.query,
            files=filtered_files,
            elapsed_ms=result.elapsed_ms,
            engine_used=result.engine_used,
            error=result.error,
        )

    def _rule_matches(self, rule: str, path_text: str) -> bool:
        if not rule:
            return False
        if self.use_regex:
            try:
                return re.search(rule, path_text) is not None
            except re.error:
                return False
        # Absolute path rule (e.g. from context-menu "Exclude directory"): match the exact
        # path OR any file whose path starts with that directory prefix.
        rule_path = Path(rule)
        if rule_path.is_absolute():
            target = Path(path_text)
            try:
                target.relative_to(rule_path)
                return True  # path is at or under the rule directory
            except ValueError:
                pass
            return path_text == rule  # exact file match
        # Glob: match against the full POSIX path OR just the filename component.
        posix_path = path_text.replace("\\", "/")
        posix_rule = rule.replace("\\", "/")
        if fnmatch.fnmatch(posix_path, posix_rule) or fnmatch.fnmatch(Path(path_text).name, posix_rule):
            return True
        # Relative path-containing globs (e.g. "src/**", "src/osprey/*.py") are applied by
        # rg relative to its search root, so they never match full absolute file paths in a
        # literal fnmatch.  Try matching the glob against every suffix of the path.
        #
        # Two strategies based on whether the rule contains "**":
        #   "**" present (e.g. "src/**"):  prepend "*/" and let fnmatch's * handle any depth.
        #   No "**"   (e.g. "src/*.py"):   component-by-component to prevent * crossing "/".
        if "/" in posix_rule:
            if "**" in posix_rule:
                # "*" in fnmatch matches "/" so "*/src/**" matches any absolute path under src/.
                if fnmatch.fnmatch(posix_path, "*/" + posix_rule):
                    return True
            else:
                # Component-by-component: honour gitignore semantics where * stays within one dir.
                rule_parts = posix_rule.split("/")
                path_parts = posix_path.split("/")
                n = len(rule_parts)
                for i in range(len(path_parts) - n + 1):
                    if all(
                        fnmatch.fnmatch(pp, rp)
                        for pp, rp in zip(path_parts[i : i + n], rule_parts)
                    ):
                        return True
        return False

    def to_rg_args(self) -> list[str]:
        """Convert filter settings to ripgrep CLI arguments."""
        args: list[str] = []
        if not self.use_regex:
            for glob in self.include_rules:
                # Absolute directory paths are NOT passed here — _build_command() in
                # RipgrepEngine promotes them to positional search-path arguments because
                # rg -g anchors directory globs to the path start and they never match
                # full absolute file paths.  Only non-absolute globs (e.g. *.py) arrive here.
                # rg requires "/" on all platforms in glob patterns — normalize backslashes.
                normalized = str(Path(glob)) if Path(glob).is_absolute() else glob.replace("\\", "/")
                args += ["-g", normalized]
            for glob in self.exclude_rules:
                # Same: rg glob patterns must use "/" even on Windows.
                normalized = str(Path(glob)) if Path(glob).is_absolute() else glob.replace("\\", "/")
                args += ["-g", f"!{normalized}"]
                # Absolute directory paths need an additional /** glob so rg excludes
                # all files under that directory (not just the directory entry itself).
                if Path(glob).is_absolute():
                    args += ["-g", f"!{normalized}/**"]
        for d in self.exclude_dirs:
            args += ["--glob", f"!{d}/**"]
        if self.max_size_bytes > 0:
            args += ["--max-filesize", str(self.max_size_bytes)]
        return args


@dataclasses.dataclass
class SearchQuery:
    """All parameters that describe a single search request."""

    pattern: str
    paths: list[Path]
    file_filter: FileFilter = dataclasses.field(default_factory=FileFilter)
    regex: bool = False
    case_sensitive: bool = True
    whole_word: bool = False
    # 0 means unlimited
    max_results: int = 0
    context_lines: int = 0
    # True when the user clicked "Find Files" — engines return file paths only (no line content)
    file_only_mode: bool = False


@dataclasses.dataclass
class Match:
    """A single match within a line of a file."""

    line_number: int
    column_start: int
    column_end: int
    line_text: str
    match_text: str


@dataclasses.dataclass
class FileMatches:
    """All matches found within a single file."""

    file_path: Path
    encoding: str
    matches: list[Match]

    @property
    def match_count(self) -> int:
        return len(self.matches)


@dataclasses.dataclass
class SearchResult:
    """Complete result returned by a SearchEngine after one search run."""

    query: SearchQuery
    files: list[FileMatches]
    elapsed_ms: float
    engine_used: str
    error: str | None = None

    @property
    def total_match_count(self) -> int:
        return sum(f.match_count for f in self.files)

    @property
    def total_file_count(self) -> int:
        return len(self.files)

    @property
    def is_ok(self) -> bool:
        return self.error is None


@runtime_checkable
class SearchEngine(Protocol):
    """Protocol every search backend must satisfy."""

    def search(self, query: SearchQuery) -> SearchResult:
        """Execute the query and return a SearchResult."""
        ...

    def name(self) -> str:
        """Human-readable engine name (e.g. 'ripgrep')."""
        ...

    def is_available(self) -> bool:
        """Return True if the backend binary/command exists on this system."""
        ...

    def version(self) -> str:
        """Return the backend version string, or '' if unavailable."""
        ...
