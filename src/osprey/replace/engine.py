"""
osprey.replace.engine
~~~~~~~~~~~~~~~~~~~~~
Replace engine: preview diffs, apply replacements with optional backup,
and undo the last replace session.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import re
import shutil
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_APP_NAME = "osprey"

# Minimal UUID v4 generator: avoids importing the `uuid` stdlib module.
def _new_session_id() -> str:
    """Return a random UUID v4 string (same format as str(uuid.uuid4()))."""
    b = bytearray(os.urandom(16))
    b[6] = (b[6] & 0x0F) | 0x40  # version 4
    b[8] = (b[8] & 0x3F) | 0x80  # variant RFC 4122
    h = b.hex()
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"
_SESSION_DIR_NAME = "replace-sessions"


def _default_config_dir() -> Path:
    """Return the OS-appropriate user config directory for osprey."""
    try:
        from platformdirs import user_config_dir  # type: ignore[import]

        return Path(user_config_dir(_APP_NAME))
    except ImportError:
        return Path.home() / f".{_APP_NAME}"


def _session_store_dir(config_dir: Path | None = None) -> Path:
    """Return the directory used to persist replace sessions."""
    return (config_dir or _default_config_dir()) / _SESSION_DIR_NAME


@dataclasses.dataclass
class DiffChange:
    """A single line-level change produced by a replace operation."""

    line_number: int
    old_text: str
    new_text: str


@dataclasses.dataclass
class FileDiff:
    """All changes within one file."""

    file_path: Path
    original_lines: list[str]
    patched_lines: list[str]
    changes: list[DiffChange]

    @property
    def change_count(self) -> int:
        return len(self.changes)


@dataclasses.dataclass
class ReplaceSession:
    """Represents one committed replace operation — supports rollback."""

    session_id: str
    timestamp: datetime
    diffs: list[FileDiff]
    # Maps original path -> backup path
    backup_paths: dict[str, str]
    is_committed: bool = False


class ReplaceEngine:
    """
    Core replace logic.

    Usage::
        engine = ReplaceEngine()
        diffs = engine.preview("foo", "bar", [Path("a.py")])
        session = engine.apply(diffs, backup=True)
        engine.undo(session)   # restore originals
    """

    def __init__(self, backup_suffix: str = ".osprey.bak") -> None:
        self._backup_suffix = backup_suffix

    def preview(
        self,
        pattern: str,
        replacement: str,
        files: list[Path],
        *,
        regex: bool = False,
        case_sensitive: bool = True,
    ) -> list[FileDiff]:
        """Return diffs for all files without writing anything to disk."""
        diffs: list[FileDiff] = []

        flags = 0 if case_sensitive else re.IGNORECASE
        compiled = re.compile(pattern if regex else re.escape(pattern), flags)

        for path in files:
            try:
                original_lines = path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
            except OSError as exc:
                logger.warning("[Replace] Cannot read %s: %s", path, exc)
                continue

            patched_lines: list[str] = []
            changes: list[DiffChange] = []

            for idx, line in enumerate(original_lines, start=1):
                new_line = compiled.sub(replacement, line)
                patched_lines.append(new_line)
                if new_line != line:
                    changes.append(DiffChange(line_number=idx, old_text=line, new_text=new_line))

            if changes:
                diffs.append(FileDiff(
                    file_path=path,
                    original_lines=original_lines,
                    patched_lines=patched_lines,
                    changes=changes,
                ))

        logger.debug("[Replace] Preview: %d files with changes", len(diffs))
        return diffs

    def apply_partial(
        self,
        file_diff: FileDiff,
        changes: list[DiffChange],
        *,
        backup: bool = True,
    ) -> bool:
        """Apply a *subset* of changes from *file_diff* to the file on disk.

        Reads the current file content, applies only the lines in *changes*
        (matched by ``line_number`` **and** ``old_text`` to guard against
        stale diffs caused by previous partial applies), and writes back.

        Returns ``True`` when at least one change was successfully written.
        """
        path = file_diff.file_path
        try:
            current_lines = path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
        except OSError as exc:
            logger.error("[Replace] apply_partial: cannot read %s: %s", path, exc)
            return False

        if backup:
            bak_path = path.with_suffix(path.suffix + self._backup_suffix)
            try:
                shutil.copy2(str(path), str(bak_path))
                logger.info("[Replace] Backup: %s -> %s", path, bak_path)
            except OSError as exc:
                logger.error("[Replace] Backup failed for %s: %s", path, exc)
                return False

        applied = 0
        for change in changes:
            idx = change.line_number - 1  # DiffChange line_number is 1-based
            if 0 <= idx < len(current_lines) and current_lines[idx] == change.old_text:
                current_lines[idx] = change.new_text
                applied += 1
            else:
                logger.warning(
                    "[Replace] apply_partial: line %d in %s mismatch — skipping "
                    "(expected %r, found %r)",
                    change.line_number,
                    path,
                    change.old_text,
                    current_lines[idx] if 0 <= idx < len(current_lines) else "<out of range>",
                )

        if applied == 0:
            logger.warning("[Replace] apply_partial: no matching lines in %s", path)
            return False

        try:
            path.write_text("".join(current_lines), encoding="utf-8")
            logger.info(
                "[Replace] apply_partial: %d/%d changes written to %s",
                applied, len(changes), path,
            )
            return True
        except OSError as exc:
            logger.error("[Replace] apply_partial: write failed for %s: %s", path, exc)
            return False

    def apply(self, diffs: list[FileDiff], *, backup: bool = True) -> ReplaceSession:
        """Write patched content to disk and return a ReplaceSession for undo."""
        session = ReplaceSession(
            session_id=_new_session_id(),
            timestamp=datetime.utcnow(),
            diffs=diffs,
            backup_paths={},
        )

        for diff in diffs:
            path = diff.file_path
            if backup:
                bak_path = path.with_suffix(path.suffix + self._backup_suffix)
                try:
                    shutil.copy2(str(path), str(bak_path))
                    session.backup_paths[str(path)] = str(bak_path)
                    logger.info("[Replace] Backup: %s -> %s", path, bak_path)
                except OSError as exc:
                    logger.error("[Replace] Backup failed for %s: %s", path, exc)
                    continue

            try:
                path.write_text("".join(diff.patched_lines), encoding="utf-8")
                logger.info("[Replace] Applied %d changes to %s", diff.change_count, path)
            except OSError as exc:
                logger.error("[Replace] Write failed for %s: %s", path, exc)

        session.is_committed = True
        self.save_session(session)
        return session

    def undo(self, session: ReplaceSession) -> bool:
        """Restore original files from the backup paths recorded in session."""
        if not session.is_committed:
            logger.warning("[Replace] Session %s was not committed, nothing to undo", session.session_id)
            return False

        success = True
        for original_path_str, backup_path_str in session.backup_paths.items():
            try:
                shutil.copy2(backup_path_str, original_path_str)
                Path(backup_path_str).unlink(missing_ok=True)
                logger.info("[Replace] Restored %s from backup", original_path_str)
            except OSError as exc:
                logger.error("[Replace] Undo failed for %s: %s", original_path_str, exc)
                success = False

        return success

    def save_session(self, session: ReplaceSession, config_dir: Path | None = None) -> Path | None:
        """Persist a replace session to disk for later undo or inspection."""
        store_dir = _session_store_dir(config_dir)
        store_dir.mkdir(parents=True, exist_ok=True)
        path = store_dir / f"{session.session_id}.json"

        try:
            path.write_text(json.dumps(self._session_to_record(session), indent=2), encoding="utf-8")
            logger.info("[Replace] Session saved to %s", path)
            return path
        except OSError as exc:
            logger.error("[Replace] Failed to save session %s: %s", session.session_id, exc)
            return None

    def load_session(self, session_id: str, config_dir: Path | None = None) -> ReplaceSession | None:
        """Load one persisted replace session from disk."""
        path = _session_store_dir(config_dir) / f"{session_id}.json"
        if not path.exists():
            logger.debug("[Replace] No persisted session found for %s", session_id)
            return None

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return self._session_from_record(data)
        except Exception as exc:
            logger.error("[Replace] Failed to load session %s: %s", session_id, exc)
            return None

    def list_sessions(self, config_dir: Path | None = None) -> list[ReplaceSession]:
        """Return all persisted replace sessions, newest first."""
        store_dir = _session_store_dir(config_dir)
        if not store_dir.exists():
            return []

        sessions: list[ReplaceSession] = []
        for path in sorted(store_dir.glob("*.json"), reverse=True):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                sessions.append(self._session_from_record(data))
            except Exception as exc:
                logger.warning("[Replace] Skipping invalid session file %s: %s", path, exc)
        return sessions

    def _session_to_record(self, session: ReplaceSession) -> dict:
        return {
            "session_id": session.session_id,
            "timestamp": session.timestamp.isoformat(),
            "is_committed": session.is_committed,
            "backup_paths": session.backup_paths,
            "diffs": [
                {
                    "file_path": str(diff.file_path),
                    "original_lines": diff.original_lines,
                    "patched_lines": diff.patched_lines,
                    "changes": [dataclasses.asdict(change) for change in diff.changes],
                }
                for diff in session.diffs
            ],
        }

    def _session_from_record(self, record: dict) -> ReplaceSession:
        diffs: list[FileDiff] = []
        for diff_data in record.get("diffs", []):
            diffs.append(
                FileDiff(
                    file_path=Path(diff_data["file_path"]),
                    original_lines=list(diff_data.get("original_lines", [])),
                    patched_lines=list(diff_data.get("patched_lines", [])),
                    changes=[DiffChange(**change) for change in diff_data.get("changes", [])],
                )
            )

        timestamp_raw = record.get("timestamp")
        timestamp = datetime.fromisoformat(timestamp_raw) if timestamp_raw else datetime.utcnow()

        return ReplaceSession(
            session_id=record.get("session_id", _new_session_id()),
            timestamp=timestamp,
            diffs=diffs,
            backup_paths=dict(record.get("backup_paths", {})),
            is_committed=bool(record.get("is_committed", False)),
        )

    def write_audit_log(
        self,
        *,
        pattern: str,
        replacement: str,
        session: ReplaceSession,
        diffs: list[FileDiff],
        config_dir: Path | None = None,
    ) -> None:
        """Append one replace operation to the daily JSONL audit log.

        File: ``<config_dir>/history/replace-YYYY-MM-DD.jsonl``
        """
        log_dir = (config_dir or _default_config_dir()) / "history"
        log_dir.mkdir(parents=True, exist_ok=True)
        today = session.timestamp.strftime("%Y-%m-%d")
        log_file = log_dir / f"replace-{today}.jsonl"

        record = {
            "ts": session.timestamp.isoformat(),
            "session_id": session.session_id,
            "pattern": pattern,
            "replacement": replacement,
            "total_files": len(diffs),
            "total_changes": sum(d.change_count for d in diffs),
            "files": [
                {
                    "path": str(d.file_path),
                    "changes": d.change_count,
                    "backup": session.backup_paths.get(str(d.file_path)),
                }
                for d in diffs
            ],
        }
        try:
            with log_file.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
            logger.info("[Replace] Audit log updated: %s", log_file)
        except OSError as exc:
            logger.warning("[Replace] Failed to write audit log: %s", exc)
