"""
osprey.engine.grep
~~~~~~~~~~~~~~~~~~
Fallback SearchEngine backed by the system `grep` command.
Used when rg is not available.
"""

from __future__ import annotations

import logging
import re
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

# Suppress the transient console window that Win32 creates when a GUI-mode
# process spawns a console child.  0x08000000 == CREATE_NO_WINDOW.
# On POSIX creationflags=0 is accepted and has no effect.
_CREATE_NO_WINDOW: int = 0x08000000 if sys.platform == "win32" else 0


class GrepEngine:
    """Fallback engine wrapping the system `grep` binary."""

    def name(self) -> str:
        return "grep"

    def is_available(self) -> bool:
        return shutil.which("grep") is not None

    def version(self) -> str:
        try:
            result = subprocess.run(
                ["grep", "--version"],
                capture_output=True,
                text=True,
                timeout=5,
                creationflags=_CREATE_NO_WINDOW,
            )
            return result.stdout.split("\n")[0].strip()
        except Exception:
            return ""

    def search(self, query: SearchQuery) -> SearchResult:
        cmd = self._build_command(query)
        logger.info("[grep] cmd: %s", " ".join(cmd))

        start = time.monotonic()
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=60,
                creationflags=_CREATE_NO_WINDOW,
            )
        except subprocess.TimeoutExpired:
            return SearchResult(
                query=query,
                files=[],
                elapsed_ms=0.0,
                engine_used=self.name(),
                error="grep timed out",
            )
        except Exception as exc:
            return SearchResult(
                query=query,
                files=[],
                elapsed_ms=0.0,
                engine_used=self.name(),
                error=str(exc),
            )

        elapsed_ms = (time.monotonic() - start) * 1000
        files = self._parse_output(proc.stdout)

        logger.info(
            "[grep] %d files / %d matches in %.1f ms",
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

    @staticmethod
    def _build_command(query: SearchQuery) -> list[str]:
        cmd: list[str] = ["grep", "-rn", "--color=never"]

        if not query.case_sensitive:
            cmd.append("-i")
        if query.whole_word:
            cmd.append("-w")
        if not query.regex:
            cmd.append("-F")
        if query.file_only_mode:
            cmd.append("-l")

        for incl in query.file_filter.include_globs:
            cmd += ["--include", incl]
        for excl in query.file_filter.exclude_globs:
            cmd += ["--exclude", excl]
        for d in query.file_filter.exclude_dirs:
            cmd += ["--exclude-dir", d]

        cmd.append(query.pattern)
        cmd.extend(str(p) for p in query.paths)
        return cmd

    @staticmethod
    def _parse_output(stdout: str) -> list[FileMatches]:
        """Parse grep -n output: <file>:<line>:<text>"""
        file_map: dict[str, list[Match]] = {}
        # Pattern: filepath:line_number:text
        line_re = re.compile(r"^(.+?):(\d+):(.*)$")

        for raw in stdout.splitlines():
            m = line_re.match(raw)
            if not m:
                continue
            fp, lineno, text = m.group(1), int(m.group(2)), m.group(3)
            if fp not in file_map:
                file_map[fp] = []
            file_map[fp].append(
                Match(
                    line_number=lineno,
                    column_start=0,
                    column_end=0,
                    line_text=text,
                    match_text="",
                )
            )

        return [
            FileMatches(file_path=Path(fp), encoding="utf-8", matches=matches)
            for fp, matches in file_map.items()
        ]
