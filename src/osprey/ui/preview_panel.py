"""
osprey.ui.preview_panel
~~~~~~~~~~~~~~~~~~~~~~~~
Right panel: displays file content with the matched line highlighted.

Supports two display modes:

**Normal mode** (default):
    Plain-text view using :class:`QTextEdit` with a
    :class:`SearchHighlighter` that underlines / bolds every occurrence of
    the active search pattern.

**Diff mode** (activated by :meth:`show_diff_for_file`):
    Full-file HTML view with inline diff spans overlaid on every changed
    line — identical character-level rendering as the result panel.
    The syntax highlighter is detached during diff mode to avoid
    conflicting with the HTML colouring.  Calling :meth:`show_file` or
    :meth:`clear_diff_preview` returns the panel to normal mode.
"""

from __future__ import annotations

import html as _html
import logging
import re
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QFont, QSyntaxHighlighter, QTextCharFormat
from PySide6.QtWidgets import QMenu, QTextEdit, QVBoxLayout, QWidget

from osprey.ui.diff_utils import inline_diff_html as _inline_diff_html

logger = logging.getLogger(__name__)

_HIGHLIGHT_COLOR = QColor("#FFE066")  # warm yellow for match highlight
# Maximum lines for full-file diff view; larger files use a context-only view.
_FULL_VIEW_LINE_LIMIT = 3_000
_DIFF_CONTEXT_LINES = 5  # lines of context around each changed line in condensed view


class SearchHighlighter(QSyntaxHighlighter):
    """Highlights all occurrences of a search pattern in the preview document."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._regex_obj: re.Pattern | None = None
        self._fmt = QTextCharFormat()
        self._fmt.setBackground(_HIGHLIGHT_COLOR)
        self._fmt.setFontWeight(QFont.Weight.Bold)

    def set_pattern(
        self,
        pattern: str,
        *,
        regex: bool = False,
        case_sensitive: bool = True,
        whole_word: bool = False,
    ) -> None:
        self._regex_obj = None
        if pattern:
            flags = 0 if case_sensitive else re.IGNORECASE
            pat = pattern if regex else re.escape(pattern)
            if whole_word:
                pat = rf"\b{pat}\b"
            try:
                self._regex_obj = re.compile(pat, flags)
            except re.error:
                self._regex_obj = re.compile(re.escape(pattern), flags)
        self.rehighlight()

    def highlightBlock(self, text: str) -> None:
        if self._regex_obj is None:
            return
        for m in self._regex_obj.finditer(text):
            self.setFormat(m.start(), m.end() - m.start(), self._fmt)


class PreviewPanel(QWidget):
    """File content viewer with match highlighting and diff overlay support."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._current_pattern: str = ""
        # Track current file so we can restore normal view after diff mode exits.
        self._current_file: Path | None = None
        self._current_line: int = 1
        # Diff mode flag — True while HTML diff overlay is shown.
        self._diff_mode: bool = False
        # Whether soft line-wrap is active (toggled via the preview bar checkbox).
        self._line_wrap: bool = False
        # Path of the file currently rendered in diff mode (None in normal mode).
        self._diff_file_path: Path | None = None
        # External editors list supplied by MainWindow from AppSettings.
        self._editors: list = []
        # Whether to prepend 1-based line numbers to each line (normal mode only).
        self._show_line_numbers: bool = True
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        # QTextEdit (instead of QPlainTextEdit) is required to support setHtml()
        # for the diff overlay.  Both share the same QTextDocument API.
        self._editor = QTextEdit()
        self._editor.setReadOnly(True)
        self._editor.setFont(QFont("Courier New", 11))
        self._editor.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        # Custom context menu so we can inject "Open with …" editor actions.
        self._editor.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._editor.customContextMenuRequested.connect(self._on_context_menu)
        layout.addWidget(self._editor)

        self._highlighter = SearchHighlighter(self._editor.document())

    # ------------------------------------------------------------------
    # Public API — normal mode
    # ------------------------------------------------------------------

    def show_file(self, path: Path, line_number: int) -> None:
        """Load file content and scroll to *line_number* (normal mode).

        If the panel was in diff mode, exits diff mode and re-attaches the
        syntax highlighter before loading the file.
        """
        # Exit diff mode: re-attach the highlighter to the document.
        if self._diff_mode:
            self._diff_mode = False
            self._diff_file_path = None
            self._highlighter.setDocument(self._editor.document())

        self._current_file = path
        self._current_line = max(line_number, 1)

        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            logger.warning("[Preview] Cannot read %s: %s", path, exc)
            self._editor.setPlainText(f"Cannot open file: {exc}")
            return

        # Optionally prepend line numbers (plain-text / non-diff mode only).
        display_text = self._format_with_line_numbers(text) if self._show_line_numbers else text
        self._editor.setPlainText(display_text)
        self._highlighter.rehighlight()
        self._scroll_to_line(self._current_line)
        logger.debug("[Preview] Loaded %s (L%d, line_numbers=%s)", path.name, line_number, self._show_line_numbers)

    def set_highlight_pattern(
        self,
        pattern: str,
        *,
        regex: bool = False,
        case_sensitive: bool = True,
        whole_word: bool = False,
    ) -> None:
        """Update the term being highlighted in normal preview mode."""
        self._current_pattern = pattern
        self._highlighter.set_pattern(
            pattern, regex=regex, case_sensitive=case_sensitive, whole_word=whole_word
        )

    def set_match_pattern(
        self,
        pattern: str,
        *,
        regex: bool = False,
        case_sensitive: bool = True,
        whole_word: bool = False,
    ) -> None:
        """Alias for :meth:`set_highlight_pattern` — used by MainWindow."""
        self.set_highlight_pattern(
            pattern, regex=regex, case_sensitive=case_sensitive, whole_word=whole_word
        )

    def set_editors(self, editors: list) -> None:
        """Replace the list of editors shown in the context menu.

        *editors* should be a list of :class:`~osprey.config.settings.EditorConfig`
        instances (or any object with ``.name`` and ``.open_file(path, line)``
        attributes).  Pass an empty list to hide the "Open with…" section.
        """
        self._editors = list(editors)
        logger.debug("[Preview] Editors updated: %d configured", len(self._editors))

    def set_line_wrap(self, enabled: bool) -> None:
        """Enable or disable soft line-wrap in the preview editor.

        When *enabled* is ``True`` the editor wraps at the widget width and
        the horizontal scrollbar is hidden.  When ``False`` (default) the
        editor uses no-wrap mode and the horizontal scrollbar reappears.
        """
        if self._line_wrap == enabled:
            return
        self._line_wrap = enabled
        if enabled:
            self._editor.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
            self._editor.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        else:
            self._editor.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
            self._editor.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        logger.debug("[Preview] line_wrap -> %s", enabled)

    def set_show_line_numbers(self, show: bool) -> None:
        """Enable or disable line-number prefixes in normal (non-diff) mode.

        If a file is currently visible the view is refreshed immediately so
        the change takes effect without the user having to re-click.
        """
        if self._show_line_numbers == show:
            return
        self._show_line_numbers = show
        logger.debug("[Preview] show_line_numbers -> %s", show)
        # Re-render the current file so the change is visible immediately.
        if not self._diff_mode and self._current_file is not None:
            self.show_file(self._current_file, self._current_line)

    # ------------------------------------------------------------------
    # Public API — diff mode
    # ------------------------------------------------------------------

    @property
    def diff_file_path(self) -> "Path | None":
        """Return the file path currently rendered in diff mode, or ``None``."""
        return self._diff_file_path

    def show_diff_for_file(
        self,
        file_diff,
        *,
        highlighted: frozenset[int] | None = None,
        scroll_to: int = 0,
    ) -> None:
        """Render file content with inline replace diff overlay (diff mode).

        *file_diff* must be a :class:`~osprey.replace.engine.FileDiff`
        object whose ``original_lines`` hold the current on-disk content
        and whose ``changes`` list contains the *pending* replacements.

        *highlighted* is an optional set of 1-based line numbers that have
        already been applied to disk.  These lines are rendered with the
        same yellow background as search-match highlighting so the user can
        see exactly what was replaced.  Lines in *highlighted* are NOT in
        ``file_diff.changes`` — they have already been committed.

        *scroll_to* is a 1-based line number to scroll to after rendering.
        When non-zero it takes priority over the default first-change scroll.
        Pass the clicked diff_change line number so the user lands on the
        specific change they selected rather than always the first change.

        The syntax highlighter is detached before calling ``setHtml()`` to
        prevent it from corrupting the HTML colour spans.
        """
        hl = highlighted or frozenset()

        # Detach syntax highlighter on first entry into diff mode.
        if not self._diff_mode:
            self._highlighter.setDocument(None)
            self._diff_mode = True

        # Track which file is currently shown in diff mode so that
        # _sync_preview_after_change can detect whether the panel is
        # already displaying the relevant file (independent of whether
        # the user has clicked on a result row).
        self._diff_file_path = file_diff.file_path

        html = self._build_diff_html(file_diff, hl)
        self._editor.setHtml(html)

        # Determine scroll target: caller-specified line > first pending > first applied.
        target_line: int | None = None
        if scroll_to > 0:
            target_line = scroll_to
        elif file_diff.changes:
            target_line = file_diff.changes[0].line_number
        elif hl:
            target_line = min(hl)
        if target_line is not None:
            _anchor = f"diff-L{target_line}"
            _line = target_line
            # Defer 50 ms so Qt completes the HTML layout pass before we read
            # blockBoundingRect() or the scrollbar maximum.  singleShot(0)
            # fires after one event tick which is usually enough, but 50 ms
            # gives the document layout scheduler a full paint cycle to finish.
            QTimer.singleShot(
                50,
                lambda _a=_anchor, _l=_line: self._scroll_diff_to_line(_a, _l),
            )

        logger.debug(
            "[Preview] Diff overlay: %s (%d pending, %d highlighted, scroll_to=%d)",
            file_diff.file_path.name,
            len(file_diff.changes),
            len(hl),
            scroll_to,
        )

    def clear_diff_preview(self) -> None:
        """Exit diff mode and return to normal file view.

        If the panel was showing a file before diff mode was activated,
        that file is reloaded.  Otherwise the panel is cleared.
        """
        if not self._diff_mode:
            return
        self._diff_mode = False
        self._diff_file_path = None
        self._highlighter.setDocument(self._editor.document())
        if self._current_file is not None:
            self.show_file(self._current_file, self._current_line)
        else:
            self._editor.clear()
        logger.debug("[Preview] Exited diff mode")

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _format_with_line_numbers(self, text: str) -> str:
        """Return *text* with each line prefixed by a right-aligned line number.

        The number width is computed from the total line count so all prefixes
        have the same length (avoids content shifting when scrolling).

        Example output (5-line file)::

            1 │ def foo():
            2 │     pass

        This method is only called in normal mode; the diff overlay already
        renders its own ``ln_prefix`` HTML spans.
        """
        lines = text.splitlines(keepends=True)
        if not lines:
            return text
        width = len(str(len(lines)))
        return "".join(f"{i:{width}d} \u2502 {line}" for i, line in enumerate(lines, 1))

    def _scroll_diff_to_line(self, anchor_name: str, target_line: int) -> None:
        """Scroll the diff view so *target_line* is centered in the viewport.

        Runs inside a ``QTimer.singleShot(50)`` callback so HTML layout is
        complete when this executes.  Strategy:

        1. Scan QTextDocument fragments to find the anchor and record its block
           Y coordinate from ``blockBoundingRect()``.
        2. If found, center that Y directly via the vertical scrollbar.
        3. If not found (e.g. Qt did not propagate the anchor name through the
           char format), fall back to a font-metric estimate so the user always
           lands near the correct line.
        """
        vbar = self._editor.verticalScrollBar()
        doc  = self._editor.document()
        max_val = vbar.maximum()
        half_vp = self._editor.viewport().height() // 2

        logger.info(
            "[Preview:diff-scroll] anchor=%r target_line=%d | "
            "vbar(min=%d max=%d pageStep=%d val=%d) | "
            "viewport_h=%d | doc(blocks=%d chars=%d)",
            anchor_name, target_line,
            vbar.minimum(), max_val, vbar.pageStep(), vbar.value(),
            self._editor.viewport().height(),
            doc.blockCount(), doc.characterCount(),
        )

        # --- Phase 1: scan QTextFragments for the anchor ---
        found_y: float = -1.0
        sample_anchors: list[str] = []

        for bn in range(doc.blockCount()):
            block = doc.findBlockByNumber(bn)
            if not block.isValid():
                continue
            it = block.begin()
            while not it.atEnd():
                frag = it.fragment()
                fmt  = frag.charFormat()
                if fmt.isAnchor():
                    names = fmt.anchorNames()
                    if anchor_name in names:
                        try:
                            found_y = doc.documentLayout().blockBoundingRect(block).top()
                        except Exception:
                            found_y = -1.0
                        break
                    if len(sample_anchors) < 8:
                        sample_anchors.extend(names)
                it += 1
            if found_y >= 0:
                break

        # --- Phase 2: center using the anchor's exact Y coordinate ---
        if found_y >= 0:
            new_val = max(0, min(int(found_y) - half_vp, max_val))
            vbar.setValue(new_val)
            logger.info(
                "[Preview:diff-scroll] anchor found at y=%.1f → scrollbar=%d (max=%d)",
                found_y, new_val, max_val,
            )
            return

        # --- Phase 3: font-metric fallback ---
        # Anchor not found in fragment scan (e.g. char format did not carry
        # the anchor name through a nested inline element).  Estimate line Y
        # from font metrics — accurate for the fixed monospace <pre> block.
        logger.warning(
            "[Preview:diff-scroll] anchor %r NOT FOUND in %d blocks. "
            "Sample anchors present: %s — using font-metric fallback.",
            anchor_name, doc.blockCount(), sample_anchors,
        )
        if max_val <= 0:
            logger.warning(
                "[Preview:diff-scroll] scrollbar max=0 — layout not complete; "
                "scroll skipped"
            )
            return
        fm = self._editor.fontMetrics()
        line_h = fm.lineSpacing()
        # 4 px top padding matches the <pre> block padding in _build_*_diff_html
        target_y = 4 + (target_line - 1) * line_h
        new_val  = max(0, min(target_y - half_vp, max_val))
        vbar.setValue(new_val)
        logger.info(
            "[Preview:diff-scroll] font-metric fallback: "
            "line_h=%d target_y=%d half_vp=%d new_val=%d max=%d",
            line_h, target_y, half_vp, new_val, max_val,
        )

    def _on_context_menu(self, pos) -> None:
        """Build and show the context menu for the editor widget.

        Prepends standard QTextEdit actions, then appends an \"Open with …\"
        section for every configured external editor (if any).
        """
        menu: QMenu = self._editor.createStandardContextMenu()

        if self._current_file and self._editors:
            menu.addSeparator()
            for ed in self._editors:
                action = menu.addAction(f"Open with {ed.name}")
                # Capture loop variable explicitly to avoid late-binding closure.
                action.triggered.connect(
                    lambda checked=False, _ed=ed, _f=self._current_file, _l=self._current_line:
                        _ed.open_file(_f, _l if _l > 0 else None)
                )

        menu.exec(self._editor.mapToGlobal(pos))

    def _build_diff_html(self, file_diff, highlighted: frozenset[int]) -> str:
        """Build an HTML document showing *file_diff.original_lines* with
        inline diff spans on pending changed lines and yellow highlights on
        applied lines.

        For files with more than :data:`_FULL_VIEW_LINE_LIMIT` lines only
        the changed / highlighted lines and their surrounding context are
        shown to keep rendering fast.
        """
        lines = file_diff.original_lines
        if len(lines) > _FULL_VIEW_LINE_LIMIT:
            return self._build_condensed_diff_html(file_diff, highlighted)
        return self._build_full_diff_html(file_diff, lines, highlighted)

    def _build_full_diff_html(self, file_diff, lines: list[str], highlighted: frozenset[int]) -> str:
        """Full file content with diff/highlight spans — suitable for small/medium files."""
        change_map = {c.line_number: c for c in file_diff.changes}
        parts: list[str] = [
            "<html><body>"
            "<pre style=\"font-family:'Courier New',Courier,monospace;"
            "font-size:11pt;margin:0;padding:4px;\">"
        ]
        for idx, raw_line in enumerate(lines, start=1):
            parts.append(self._render_line(idx, raw_line, change_map, highlighted))
        parts.append("</pre></body></html>")
        return "".join(parts)

    def _build_condensed_diff_html(self, file_diff, highlighted: frozenset[int]) -> str:
        """Context-only view for large files — shows changed/highlighted lines ± context."""
        lines = file_diff.original_lines
        change_map = {c.line_number: c for c in file_diff.changes}
        # Include both pending and applied lines (+ context) in the visible set.
        show: set[int] = set()
        for ln in (*change_map, *highlighted):
            for i in range(max(1, ln - _DIFF_CONTEXT_LINES),
                           min(len(lines) + 1, ln + _DIFF_CONTEXT_LINES + 1)):
                show.add(i)

        parts: list[str] = [
            "<html><body>"
            "<pre style=\"font-family:'Courier New',Courier,monospace;"
            "font-size:11pt;margin:0;padding:4px;\">"
            f"<span style=\"color:#888888;font-style:italic;\">"
            f"[Large file: {len(lines)} lines — showing {len(file_diff.changes)} "
            f"pending + {len(highlighted)} applied change(s) "
            f"with {_DIFF_CONTEXT_LINES}-line context]</span>\n"
            "<span style=\"color:#888888;\">────────────────────────────────</span>\n"
        ]
        prev_shown: int = 0
        for idx in sorted(show):
            if prev_shown and idx > prev_shown + 1:
                parts.append(
                    f"<span style=\"color:#aaaaaa;\">  ···  "
                    f"({idx - prev_shown - 1} line(s) omitted)  ···</span>\n"
                )
            raw_line = lines[idx - 1]  # idx is 1-based
            parts.append(self._render_line(idx, raw_line, change_map, highlighted))
            prev_shown = idx
        parts.append("</pre></body></html>")
        return "".join(parts)

    def _render_line(self, idx: int, raw_line: str, change_map: dict, highlighted: frozenset[int]) -> str:
        """Render a single file line as an HTML string (with anchor for scrolling).

        - Pending change line (in *change_map*): red/green inline diff on pink background.
        - Applied line (in *highlighted*): new text with yellow background (match style).
        - Normal line: plain HTML-escaped text.

        The scroll anchor ``<a name="diff-L{idx}">`` wraps the line-number text so it
        is **non-empty**.  Qt's ``QTextEdit.scrollToAnchor()`` locates anchors by
        iterating ``QTextFragment`` objects; an empty ``<a name="..."></a>`` tag
        produces no fragment and is therefore never found — the anchor must contain
        at least one character of text content.
        """
        line_text = raw_line.rstrip("\r\n")
        # Anchor with styling on the <a> itself (no nested <span>).
        # Qt's HTML parser applies the <a name> to the char format of the
        # enclosed text.  A nested <span> would push a new char format that
        # may not inherit the anchor name, causing anchorPosition() to miss it.
        ln_prefix = (
            f'<a name="diff-L{idx}" style="color:#aaaaaa;font-size:9pt;'
            f'font-family:monospace;text-decoration:none;">'
            f"{idx:5d}  </a>"
        )
        if idx in change_map:
            change = change_map[idx]
            diff_span = _inline_diff_html(
                change.old_text,
                change.new_text,
                old_color="#cc2222",
                new_color="#1a8a1a",
                base_color="#333333",
            )
            return (
                f'<span style="background-color:#fff0f0;">'
                f"{ln_prefix}{diff_span}</span>\n"
            )
        if idx in highlighted:
            # Applied line — new text already on disk; highlight in search-match style.
            return (
                f'<span style="background-color:#FFF9C4;">'
                f"{ln_prefix}<b>{_html.escape(line_text)}</b></span>\n"
            )
        return f"{ln_prefix}{_html.escape(line_text)}\n"

    def _scroll_to_line(self, line_number: int) -> None:
        """Scroll plain-text view so *line_number* (1-based) is centered vertically.

        ``QTextEdit`` has no ``centerCursor()`` slot (that exists only on
        ``QPlainTextEdit``).  Strategy:

        1. Move the cursor to the target block and call
           ``ensureCursorVisible()`` so Qt performs lazy layout and an
           initial scroll that brings the line into view (at the edge).
        2. Read the cursor's actual viewport Y via ``cursorRect()``.
        3. Compute the signed delta to the viewport midpoint and apply it to
           the vertical scrollbar once.  The scrollbar clamps automatically,
           so lines near the file edges are shown as centered as possible.
        """
        if line_number <= 0:
            return
        doc = self._editor.document()
        block = doc.findBlockByLineNumber(line_number - 1)
        if not block.isValid():
            return

        cursor = self._editor.textCursor()
        cursor.setPosition(block.position())
        self._editor.setTextCursor(cursor)
        # Step 1: ensure Qt has laid out the block and scrolled it into view.
        self._editor.ensureCursorVisible()

        # Step 2-3: shift scrollbar so the cursor sits at the viewport midpoint.
        # delta > 0  → cursor is below center → scroll down (move doc upward)
        # delta < 0  → cursor is above center → scroll up  (move doc downward)
        cursor_rect = self._editor.cursorRect()
        vbar = self._editor.verticalScrollBar()
        delta = cursor_rect.top() - self._editor.viewport().height() // 2
        vbar.setValue(vbar.value() + delta)

