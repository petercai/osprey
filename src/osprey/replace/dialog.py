"""
osprey.replace.dialog
~~~~~~~~~~~~~~~~~~~~~
ReplaceDialog: a QDialog that shows a unified-diff preview of all pending
replacements, then lets the user apply them in one batch.

Workflow
--------
1. Caller creates the dialog with a list of FileDiff objects.
2. User reviews the unified-diff view (red=removed, green=added).
3. User clicks "Apply All" to commit, or "Cancel" to discard.
4. Caller checks dialog.result() / dialog.exec() return value.
"""

from __future__ import annotations

import difflib
import logging
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QSyntaxHighlighter, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QPlainTextEdit,
    QVBoxLayout,
)

from osprey.replace.engine import FileDiff

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Diff-view syntax highlighter
# ---------------------------------------------------------------------------

class _DiffHighlighter(QSyntaxHighlighter):
    """
    Highlights unified diff output inside a QPlainTextEdit:
      - Lines starting with '-'  → red background (removed)
      - Lines starting with '+'  → green background (added)
      - Lines starting with '@@' → grey italic (hunk header)
    Search-term highlighting is intentionally not applied here; the caller
    should supply diffs that already embed the match context.
    """

    _FMT_REMOVED = QTextCharFormat()
    _FMT_ADDED = QTextCharFormat()
    _FMT_HUNK = QTextCharFormat()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._FMT_REMOVED.setBackground(QColor("#ffcccc"))
        self._FMT_ADDED.setBackground(QColor("#ccffcc"))
        self._FMT_HUNK.setForeground(QColor("#6a6a6a"))
        font = QFont()
        font.setItalic(True)
        self._FMT_HUNK.setFont(font)

    def highlightBlock(self, text: str) -> None:  # type: ignore[override]
        if text.startswith("-") and not text.startswith("---"):
            self.setFormat(0, len(text), self._FMT_REMOVED)
        elif text.startswith("+") and not text.startswith("+++"):
            self.setFormat(0, len(text), self._FMT_ADDED)
        elif text.startswith("@@"):
            self.setFormat(0, len(text), self._FMT_HUNK)


# ---------------------------------------------------------------------------
# Main dialog
# ---------------------------------------------------------------------------

class ReplaceDialog(QDialog):
    """
    Preview-and-confirm dialog for batch replace.

    :param diffs: List of FileDiff objects to display.
    :param pattern: Original search pattern (shown in header for context).
    :param replacement: Replacement string (shown in header).
    :param parent: Parent widget.
    """

    def __init__(
        self,
        diffs: list[FileDiff],
        pattern: str = "",
        replacement: str = "",
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._diffs = diffs
        self._pattern = pattern
        self._replacement = replacement

        self.setWindowTitle("Preview Replacements")
        self.resize(820, 600)
        self.setModal(True)

        self._build_ui()
        self._populate(diffs)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(6)

        # Summary header
        total_files = len(self._diffs)
        total_changes = sum(d.change_count for d in self._diffs)
        self._summary_label = QLabel(
            f"<b>{total_files} file(s)</b>, <b>{total_changes} replacement(s)</b>"
            f" &nbsp;·&nbsp; <code>{self._pattern!r}</code> → <code>{self._replacement!r}</code>"
        )
        self._summary_label.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(self._summary_label)

        self._note_label = QLabel(
            "This replace session will be saved to disk after commit so it can be undone later."
        )
        self._note_label.setWordWrap(True)
        layout.addWidget(self._note_label)

        # Diff viewer (read-only plain text)
        self._diff_view = QPlainTextEdit()
        self._diff_view.setReadOnly(True)
        self._diff_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        font = QFont("Menlo, Consolas, Courier New, monospace")
        font.setPointSize(10)
        self._diff_view.setFont(font)
        layout.addWidget(self._diff_view)

        # Syntax highlighter attached to the document
        self._highlighter = _DiffHighlighter(self._diff_view.document())

        # Dialog buttons
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Apply | QDialogButtonBox.StandardButton.Cancel
        )
        apply_btn = buttons.button(QDialogButtonBox.StandardButton.Apply)
        if apply_btn:
            apply_btn.setText("Apply All")
            apply_btn.setDefault(True)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    # ------------------------------------------------------------------
    # Diff rendering
    # ------------------------------------------------------------------

    def _populate(self, diffs: list[FileDiff]) -> None:
        """Render all FileDiff objects as unified diff text."""
        lines: list[str] = []

        for fd in diffs:
            file_label = str(fd.file_path)
            lines += list(
                difflib.unified_diff(
                    fd.original_lines,
                    fd.patched_lines,
                    fromfile=f"a/{file_label}",
                    tofile=f"b/{file_label}",
                    lineterm="",
                )
            )
            lines.append("")  # blank separator between files

        if not lines:
            lines = ["(no changes)"]

        self._diff_view.setPlainText("\n".join(lines))
        self._diff_view.moveCursor(QTextCursor.MoveOperation.Start)
        logger.debug(
            "[ReplaceDialog] rendered diff: %d files, %d lines",
            len(diffs), len(lines),
        )
