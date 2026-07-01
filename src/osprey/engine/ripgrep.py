"""
osprey.engine.ripgrep
~~~~~~~~~~~~~~~~~~~~~
SearchEngine implementation that delegates to the `rg` (ripgrep) binary.
Parses rg JSON output for structured, reliable result extraction.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import sys
import time
from pathlib import Path

from osprey.engine.base import (
    FileMatches,
    Match,
    SearchEngine,
    SearchQuery,
    SearchResult,
)

logger = logging.getLogger(__name__)

# On Windows, GUI-mode processes (windowed exe) cause child console processes to
# flash a console window briefly on startup.  CREATE_NO_WINDOW suppresses it.
# Value 0x08000000 is safe to pass on POSIX too — Python accepts creationflags=0.
_CREATE_NO_WINDOW: int = 0x08000000 if sys.platform == "win32" else 0


class RipgrepEngine:
    """Wraps the `rg` binary.  Uses --json output for structured parsing."""

    def __init__(self, binary: Path | None = None) -> None:
        self._binary: Path | None = binary or self._find_binary()

    # ------------------------------------------------------------------
    # SearchEngine protocol
    # ------------------------------------------------------------------

    def name(self) -> str:
        return "ripgrep"

    def is_available(self) -> bool:
        return self._binary is not None

    def version(self) -> str:
        if not self._binary:
            return ""
        try:
            result = subprocess.run(
                [str(self._binary), "--version"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
                creationflags=_CREATE_NO_WINDOW,
            )
            return result.stdout.split("\n")[0].strip()
        except Exception:
            return ""

    def search(self, query: SearchQuery) -> SearchResult:
        if not self._binary:
            return SearchResult(
                query=query,
                files=[],
                elapsed_ms=0.0,
                engine_used=self.name(),
                error="ripgrep binary not found",
            )

        cmd = self._build_command(query)
        logger.info("[ripgrep] cmd: %s", " ".join(cmd))

        start = time.monotonic()
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=60,
                creationflags=_CREATE_NO_WINDOW,
            )
        except subprocess.TimeoutExpired:
            return SearchResult(
                query=query,
                files=[],
                elapsed_ms=0.0,
                engine_used=self.name(),
                error="ripgrep timed out after 60 seconds",
            )
        except Exception as exc:
            logger.exception("[ripgrep] Unexpected error")
            return SearchResult(
                query=query,
                files=[],
                elapsed_ms=0.0,
                engine_used=self.name(),
                error=str(exc),
            )

        elapsed_ms = (time.monotonic() - start) * 1000
        if query.file_only_mode:
            files = self._parse_plain_output(proc.stdout)
        else:
            files = self._parse_json_output(proc.stdout, query)

        logger.info(
            "[ripgrep] %d files / %d matches in %.1f ms",
            len(files),
            sum(f.match_count for f in files),
            elapsed_ms,
        )
        return SearchResult(
            query=query,
            files=files,
            elapsed_ms=elapsed_ms,
            engine_used=self.name(),
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_plain_output(stdout: str | None) -> list[FileMatches]:
        """Parse plain-text rg -l output (one file path per line)."""
        if not stdout:
            return []
        return [
            FileMatches(file_path=Path(line.strip()), encoding="utf-8", matches=[])
            for line in stdout.splitlines()
            if line.strip()
        ]

    @staticmethod
    def _find_binary() -> Path | None:
        found = shutil.which("rg")
        if found:
            return Path(found)
        return None

    def _build_command(self, query: SearchQuery) -> list[str]:
        cmd: list[str] = [str(self._binary)]
        if query.file_only_mode:
            cmd.append("-l")
        else:
            cmd.append("--json")

        if not query.case_sensitive:
            cmd.append("-i")
        if query.whole_word:
            cmd.append("-w")
        if not query.regex:
            cmd.append("-F")  # fixed string (literal)
        if query.context_lines > 0:
            cmd += ["-C", str(query.context_lines)]
        if query.max_results > 0:
            cmd += ["-m", str(query.max_results)]

        # Absolute-directory include rules cannot work as rg -g globs:
        # rg anchors directory globs to the path start (regex ^dir/...), so they
        # never match full absolute file paths.  Promote them to positional search-
        # path arguments instead — that is the only reliable cross-platform approach.
        ff = query.file_filter
        effective_paths: list[Path]
        if not ff.use_regex:
            abs_dir_includes = [
                Path(r) for r in ff.include_rules if Path(r).is_absolute()
            ]
        else:
            abs_dir_includes = []

        if abs_dir_includes:
            # Build a filter that keeps only non-absolute glob rules (e.g. *.py).
            from dataclasses import replace as dc_replace  # local import avoids top-level dep
            remaining = [r for r in ff.include_rules if not Path(r).is_absolute()]
            ff = dc_replace(ff, include_rules=remaining)
            effective_paths = abs_dir_includes
            logger.debug(
                "[ripgrep] include absolute dirs → search paths: %s",
                [str(p) for p in abs_dir_includes],
            )
        else:
            effective_paths = query.paths

        cmd.extend(ff.to_rg_args())

        # Pattern + paths
        cmd.append(query.pattern)
        cmd.extend(str(p) for p in effective_paths)

        return cmd

    @staticmethod
    def _parse_json_output(stdout: str | None, query: SearchQuery) -> list[FileMatches]:
        """
        Parse rg --json output.
        Each line is a JSON object with type: 'match' | 'begin' | 'end' | 'summary'.
        """
        if not stdout:
            return []
        file_map: dict[str, list[Match]] = {}

        for raw_line in stdout.splitlines():
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                obj = json.loads(raw_line)
            except json.JSONDecodeError:
                logger.warning("[ripgrep] Unexpected non-JSON line: %s", raw_line[:120])
                continue

            if obj.get("type") != "match":
                continue

            data = obj.get("data", {})
            path_text = data.get("path", {}).get("text", "")
            lines_text = data.get("lines", {}).get("text", "")
            line_number = data.get("line_number", 0)

            submatch_list = data.get("submatches", [])
            for sub in submatch_list:
                col_start = sub.get("start", 0)
                col_end = sub.get("end", 0)
                match_text = sub.get("match", {}).get("text", "")

                if path_text not in file_map:
                    file_map[path_text] = []

                file_map[path_text].append(
                    Match(
                        line_number=line_number,
                        column_start=col_start,
                        column_end=col_end,
                        line_text=lines_text.rstrip("\n"),
                        match_text=match_text,
                    )
                )

        return [
            FileMatches(
                file_path=Path(fp),
                encoding="utf-8",
                matches=matches,
            )
            for fp, matches in file_map.items()
        ]
