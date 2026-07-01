"""
osprey.ui.result_panel
~~~~~~~~~~~~~~~~~~~~~~
Center panel: QTreeView showing search results grouped by file.
Emits item_selected(file_path, line_number) when user clicks a match row.

Includes an inline content filter bar (in-memory substring filter with 200ms debounce).
"""

from __future__ import annotations

import html as _html
import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

from PySide6.QtCore import QAbstractItemModel, QModelIndex, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontDatabase, QIcon, QPainter, QPixmap, QTextDocument
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QStyle,
    QStyledItemDelegate,
    QCheckBox,
    QToolButton,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from osprey.engine.base import FileMatches, Match, SearchResult
from osprey.ui.diff_utils import inline_diff_html as _inline_diff_html

# ---------------------------------------------------------------------------
# Codicons font support
# ---------------------------------------------------------------------------
# Path to the bundled Codicons TrueType font (project root / codicons / codicon.ttf).
_CODICON_TTF: Path = Path(__file__).parent.parent.parent.parent / "codicons" / "codicon.ttf"
# Lazily resolved font family name; None until first call to _ensure_codicon_font().
_codicon_family: str | None = None


def _ensure_codicon_font() -> str | None:
    """Register the Codicons TTF font once and return the font family name.

    Returns *None* if the font file is missing or Qt fails to load it,
    so callers can fall back to a system icon without crashing.
    """
    global _codicon_family  # noqa: PLW0603
    if _codicon_family is not None:
        return _codicon_family
    if not _CODICON_TTF.exists():
        logger.debug("[Codicons] TTF not found at %s", _CODICON_TTF)
        return None
    font_id = QFontDatabase.addApplicationFont(str(_CODICON_TTF))
    if font_id == -1:
        logger.warning("[Codicons] Failed to register font from %s", _CODICON_TTF)
        return None
    families = QFontDatabase.applicationFontFamilies(font_id)
    if not families:
        logger.warning("[Codicons] No font families found in %s", _CODICON_TTF)
        return None
    _codicon_family = families[0]
    logger.debug("[Codicons] Font registered: '%s' (font_id=%d)", _codicon_family, font_id)
    return _codicon_family


def _codicon_icon(codepoint: int, size: int = 16) -> QIcon:
    """Render a Codicons glyph as a *size* x *size* QIcon.

    The icon colour adapts to the current application palette (button-text
    colour) so it looks correct in both light and dark themes.

    Falls back to ``SP_DialogApplyButton`` if the Codicons font is
    unavailable (e.g. missing TTF file or Qt font-loading failure).

    Args:
        codepoint: Unicode code point as an integer, e.g. ``0xEB3D`` for
                   the *replace* glyph.
        size:      Desired icon size in pixels (default 16).
    """
    family = _ensure_codicon_font()
    if family is None:
        # Graceful degradation — no Codicons font available.
        return QApplication.style().standardIcon(QStyle.StandardPixmap.SP_DialogApplyButton)

    px = QPixmap(QSize(size, size))
    px.fill(Qt.GlobalColor.transparent)
    painter = QPainter(px)
    painter.setFont(QFont(family, size - 2))
    # Adapt to current palette so the icon looks correct in dark/light themes.
    painter.setPen(QApplication.palette().buttonText().color())
    painter.drawText(px.rect(), Qt.AlignmentFlag.AlignCenter, chr(codepoint))
    painter.end()
    return QIcon(px)


def _filter_result(query: str, result: SearchResult) -> SearchResult:
    """Return a new SearchResult keeping only items whose file path or match
    line text contains every whitespace-separated word in *query* (case-insensitive
    substring match).  No external processes are spawned."""
    words = query.lower().split()
    if not words:
        return result

    matched_files: list[FileMatches] = []
    for fm in result.files:
        file_str = fm.file_path.as_posix().lower()
        if all(w in file_str for w in words):
            # The file path itself matches every word — show all its lines.
            matched_files.append(fm)
        else:
            # Keep only the individual match lines that contain every word.
            kept = [
                m for m in fm.matches
                if all(w in m.line_text.lower() for w in words)
            ]
            if kept:
                matched_files.append(
                    FileMatches(file_path=fm.file_path, encoding=fm.encoding, matches=kept)
                )

    return SearchResult(
        query=result.query,
        files=matched_files,
        elapsed_ms=result.elapsed_ms,
        engine_used=result.engine_used,
        error=result.error,
    )


class DiffRowDelegate(QStyledItemDelegate):
    """Renders a ``diff_change`` row.

    **Pending state** (``node.applied = False``): character-level diff —
    ``L12  ~~old text~~  →  new text`` with a pink background.

    **Applied state** (``node.applied = True``): new text with a green
    background — ``L12  ✓  new text`` — after the change has been written
    to disk.  The ↩ button is hidden separately.
    """

    def paint(self, painter, option, index):
        node = index.data(Qt.ItemDataRole.UserRole)
        if node is None or node.kind != "diff_change" or index.column() != 0:
            super().paint(painter, option, index)
            return

        change = node.diff_change
        if change is None:
            super().paint(painter, option, index)
            return

        if getattr(node, "applied", False):
            self._paint_applied(painter, option, index, change)
            return

        # --- Pending diff state ---
        self.initStyleOption(option, index)
        option.text = ""  # suppress default text
        style = option.widget.style() if option.widget else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, option, painter, option.widget)

        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        old_color = "#ff8888" if selected else "#cc2222"
        new_color = "#88ee88" if selected else "#1a8a1a"
        base_color = "#dddddd" if selected else "#333333"
        prefix_color = "#bbbbbb" if selected else "#888888"

        prefix = _html.escape(f"L{change.line_number}  ")
        diff_html = _inline_diff_html(
            change.old_text,
            change.new_text,
            old_color=old_color,
            new_color=new_color,
            base_color=base_color,
        )

        html_str = (
            f'<span style="color:{prefix_color};font-family:monospace;">{prefix}</span>'
            f'<span style="font-family:monospace;">{diff_html}</span>'
        )

        doc = QTextDocument()
        doc.setHtml(html_str)
        doc.setTextWidth(option.rect.width() - 4)

        painter.save()
        painter.translate(option.rect.x() + 2, option.rect.y() + (option.rect.height() - doc.size().height()) / 2)
        doc.drawContents(painter)
        painter.restore()

    def _paint_applied(self, painter, option, index, change) -> None:
        """Render an applied diff_change row: green check + new text on light-green background."""
        self.initStyleOption(option, index)
        option.text = ""
        style = option.widget.style() if option.widget else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, option, painter, option.widget)

        new_text = _html.escape(change.new_text.rstrip("\r\n"))
        html_str = (
            f'<span style="color:#888888;font-family:monospace;">L{change.line_number}  '
            f'\u2713  </span>'
            f'<span style="color:#1a6a1a;font-family:monospace;">{new_text}</span>'
        )

        doc = QTextDocument()
        doc.setHtml(html_str)
        doc.setTextWidth(option.rect.width() - 4)

        painter.save()
        # Draw light-green background to signal "applied" state.
        painter.fillRect(option.rect, QColor("#e8f5e9"))
        painter.translate(
            option.rect.x() + 2,
            option.rect.y() + (option.rect.height() - doc.size().height()) / 2,
        )
        doc.drawContents(painter)
        painter.restore()

    def sizeHint(self, option, index):
        node = index.data(Qt.ItemDataRole.UserRole)
        if node is not None and node.kind == "diff_change":
            base = super().sizeHint(option, index)
            return QSize(base.width(), max(base.height(), 24))
        return super().sizeHint(option, index)


@dataclass
class _ResultNode:
    """Tree node for one file row or one match row."""

    kind: str
    file_path: Path | None = None
    match: Match | None = None
    matches_source: list[Match] = field(default_factory=list)
    children_loaded: bool = True
    parent: "_ResultNode | None" = None
    children: list["_ResultNode"] = field(default_factory=list)
    # Diff-mode payload (non-None only when kind starts with "diff_")
    diff_change: object = None     # DiffChange for diff_change nodes
    diff_file_data: object = None  # FileDiff for diff_file / diff_change nodes
    # True after the change has been applied to disk; changes delegate rendering.
    applied: bool = False

    def append_child(self, node: "_ResultNode") -> None:
        node.parent = self
        self.children.append(node)

    @property
    def row(self) -> int:
        if self.parent is None:
            return 0
        return self.parent.children.index(self)

    @property
    def line_number(self) -> int:
        if self.match is not None:
            return self.match.line_number
        # diff_change nodes carry the changed line number directly
        if self.diff_change is not None:
            return self.diff_change.line_number
        return 0

    @property
    def display_path(self) -> str:
        if self.file_path is None:
            return ""
        return str(self.file_path)


class ResultTreeModel(QAbstractItemModel):
    """Tree model that holds search results in a file/match hierarchy.

    Two modes, controlled by the *lazy_load* constructor flag:

    **Plain mode** (``lazy_load=False``, default):
        All file and match rows are inserted immediately on ``set_result()``.
        Simple and predictable; suitable for up to ~5,000 files.

    **Virtual / lazy mode** (``lazy_load=True``):
        Only the first FILE_CHUNK file rows are inserted at a time; match
        rows are loaded on-demand when the user expands a file row
        (``canFetchMore`` / ``fetchMore`` protocol).  A sentinel
        ``kind="more"`` node at the bottom triggers the next chunk on
        double-click.  Suitable for very large result sets.
    """

    FILE_CHUNK = 500  # file rows per chunk in virtual mode

    def __init__(self, parent=None, *, lazy_load: bool = False) -> None:
        super().__init__(parent)
        self._lazy_load = lazy_load
        self._root = _ResultNode(kind="root")
        self._pending_files: list = []  # FileMatches not yet inserted (virtual mode only)

    def clear(self) -> None:
        self.beginResetModel()
        self._root = _ResultNode(kind="root")
        self._pending_files = []
        self.endResetModel()

    def set_diff(self, diffs: list) -> None:
        """Build diff tree from a list of ``FileDiff`` objects.

        Tree structure per file::

            diff_file: /path/file.py  (N change(s))
              diff_change: L12  ~~old~~  →  new   (rendered by DiffRowDelegate)
              ...
        """
        self.beginResetModel()
        self._pending_files = []
        self._root = _ResultNode(kind="root")
        for diff in diffs:
            file_node = _ResultNode(
                kind="diff_file",
                file_path=diff.file_path,
                diff_file_data=diff,
            )
            for change in diff.changes:
                change_node = _ResultNode(
                    kind="diff_change",
                    file_path=diff.file_path,
                    diff_change=change,
                    diff_file_data=diff,
                )
                file_node.append_child(change_node)
            self._root.append_child(file_node)
        self.endResetModel()

    def remove_diff_file(self, file_path: Path) -> None:
        """Remove a diff_file node (and all its children) after it has been applied."""
        for row, child in enumerate(self._root.children):
            if child.kind == "diff_file" and child.file_path == file_path:
                self.beginRemoveRows(QModelIndex(), row, row)
                self._root.children.pop(row)
                self.endRemoveRows()
                return

    def remove_diff_change(self, file_path: Path, change_line: int) -> None:
        """Remove the ``diff_change`` node for *change_line*; remove file if empty."""
        file_node: _ResultNode | None = None
        file_row = -1
        for row, child in enumerate(self._root.children):
            if child.kind == "diff_file" and child.file_path == file_path:
                file_node = child
                file_row = row
                break
        if file_node is None:
            return

        file_parent_idx = self.createIndex(file_row, 0, file_node)
        # Collect child rows whose diff_change.line_number matches
        remove_rows = [
            i for i, c in enumerate(file_node.children)
            if c.kind == "diff_change" and c.diff_change is not None
            and c.diff_change.line_number == change_line
        ]
        # Remove in reverse order so earlier indices stay valid
        for row in sorted(remove_rows, reverse=True):
            self.beginRemoveRows(file_parent_idx, row, row)
            file_node.children.pop(row)
            self.endRemoveRows()

        # If the file has no remaining changes, remove it too
        if not file_node.children:
            self.beginRemoveRows(QModelIndex(), file_row, file_row)
            self._root.children.pop(file_row)
            self.endRemoveRows()

    def set_result(self, result: SearchResult) -> None:
        self.beginResetModel()
        self._pending_files = []
        self._root = self._build_tree(result)
        self.endResetModel()

    def _build_tree(self, result: SearchResult) -> _ResultNode:
        """Build node tree; plain mode loads everything, virtual mode chunks files."""
        root = _ResultNode(kind="root")
        all_files = result.files

        if not self._lazy_load:
            # Plain mode: insert all files and all their matches immediately
            for file_matches in all_files:
                file_node = _ResultNode(
                    kind="file",
                    file_path=file_matches.file_path,
                    matches_source=list(file_matches.matches),
                    children_loaded=True,
                )
                for match in file_matches.matches:
                    file_node.append_child(
                        _ResultNode(kind="match", file_path=file_matches.file_path, match=match)
                    )
                root.append_child(file_node)
            return root

        # Virtual mode: chunk files, lazy-load matches
        visible = all_files[: self.FILE_CHUNK]
        rest = all_files[self.FILE_CHUNK :]

        for file_matches in visible:
            file_node = _ResultNode(
                kind="file",
                file_path=file_matches.file_path,
                matches_source=list(file_matches.matches),
                children_loaded=False,
            )
            root.append_child(file_node)

        if rest:
            self._pending_files = list(rest)
            more_node = _ResultNode(
                kind="more",
                matches_source=[],
                children_loaded=True,
            )
            root.append_child(more_node)

        return root

    def load_more_files(self) -> int:
        """Insert the next FILE_CHUNK pending file nodes before the sentinel.

        Returns the number of newly inserted rows (0 when nothing left).
        """
        if not self._pending_files:
            return 0

        # Find and remove the current "more" sentinel
        sentinel_row = -1
        for i, child in enumerate(self._root.children):
            if child.kind == "more":
                sentinel_row = i
                break
        if sentinel_row == -1:
            return 0

        # Remove sentinel
        self.beginRemoveRows(QModelIndex(), sentinel_row, sentinel_row)
        self._root.children.pop(sentinel_row)
        self.endRemoveRows()

        # Insert next chunk
        batch = self._pending_files[: self.FILE_CHUNK]
        self._pending_files = self._pending_files[self.FILE_CHUNK :]

        insert_at = sentinel_row  # insert where sentinel was
        self.beginInsertRows(QModelIndex(), insert_at, insert_at + len(batch) - 1)
        for file_matches in batch:
            file_node = _ResultNode(
                kind="file",
                file_path=file_matches.file_path,
                matches_source=list(file_matches.matches),
                children_loaded=False,
            )
            file_node.parent = self._root
            self._root.children.insert(insert_at, file_node)
            insert_at += 1
        self.endInsertRows()

        # Re-append sentinel if there is still more
        if self._pending_files:
            more_node = _ResultNode(kind="more", matches_source=[], children_loaded=True)
            end_row = len(self._root.children)
            self.beginInsertRows(QModelIndex(), end_row, end_row)
            self._root.append_child(more_node)
            self.endInsertRows()

        return len(batch)

    def index(self, row: int, column: int, parent: QModelIndex = QModelIndex()) -> QModelIndex:
        if row < 0 or column < 0 or column >= self.columnCount(parent):
            return QModelIndex()

        parent_node = self._node_from_index(parent)
        if row >= len(parent_node.children):
            return QModelIndex()

        child = parent_node.children[row]
        return self.createIndex(row, column, child)

    def parent(self, index: QModelIndex) -> QModelIndex:
        if not index.isValid():
            return QModelIndex()

        node = self._node_from_index(index)
        if node.parent is None or node.parent is self._root:
            return QModelIndex()

        return self.createIndex(node.parent.row, 0, node.parent)

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        node = self._node_from_index(parent)
        # file and diff_file nodes with unloaded matches/changes report 0 until fetched
        if node.kind in ("file", "diff_file") and not node.children_loaded:
            return 0
        return len(node.children)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: ARG002
        del parent
        return 2

    def hasChildren(self, parent: QModelIndex = QModelIndex()) -> bool:
        node = self._node_from_index(parent)
        if node.kind in ("file", "diff_file"):
            return bool(node.matches_source) or bool(node.children)
        return bool(node.children)

    def canFetchMore(self, parent: QModelIndex) -> bool:
        if not self._lazy_load:
            return False  # plain mode — nothing to fetch lazily
        node = self._node_from_index(parent)
        return node.kind == "file" and not node.children_loaded and bool(node.matches_source)

    def fetchMore(self, parent: QModelIndex) -> None:
        if not self._lazy_load:
            return
        node = self._node_from_index(parent)
        if node.kind != "file" or node.children_loaded or not node.matches_source:
            return

        start = 0
        end = len(node.matches_source) - 1
        self.beginInsertRows(parent, start, end)
        for match in node.matches_source:
            node.append_child(
                _ResultNode(kind="match", file_path=node.file_path, match=match)
            )
        node.children_loaded = True
        self.endInsertRows()

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None

        node = self._node_from_index(index)
        if role == Qt.ItemDataRole.DisplayRole:
            return self._display_value(node, index.column())
        if role == Qt.ItemDataRole.ToolTipRole:
            return self._tooltip_value(node)
        if role == Qt.ItemDataRole.UserRole:
            return node
        # Diff-mode colour coding (diff_change rows are painted by DiffRowDelegate)
        if role == Qt.ItemDataRole.ForegroundRole:
            if node.kind == "diff_file":
                return QColor(50, 80, 200)    # dark blue for file headers
        return None

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole):
        if orientation != Qt.Orientation.Horizontal or role != Qt.ItemDataRole.DisplayRole:
            return None
        if section == 0:
            return "File / Line"
        if section == 1:
            # In diff mode (replace preview), show "Replace"; otherwise show "Match"
            if self._root.children and self._root.children[0].kind == "diff_file":
                return "Replace"
            return "Match"
        return None

    def flags(self, index: QModelIndex):
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        node = self._node_from_index(index)
        if node.kind == "more":
            return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

    def is_more_sentinel(self, index: QModelIndex) -> bool:
        node = self._node_from_index(index)
        return node.kind == "more"

    def node_for_index(self, index: QModelIndex) -> _ResultNode | None:
        if not index.isValid():
            return None
        return self._node_from_index(index)

    def _node_from_index(self, index: QModelIndex) -> _ResultNode:
        if index.isValid():
            node = index.internalPointer()
            if isinstance(node, _ResultNode):
                return node
        return self._root

    def _display_value(self, node: _ResultNode, column: int) -> str:
        if node.kind == "file":
            if column == 0:
                return node.display_path
            if column == 1:
                match_count = len(node.matches_source) if node.matches_source else len(node.children)
                return f"{match_count} match(es)"
            return ""

        if node.kind == "match" and node.match is not None:
            if column == 0:
                return f"L{node.match.line_number}  {node.match.line_text.strip()}"
            if column == 1:
                return node.match.match_text
            return ""

        if node.kind == "more":
            remaining = len(self._pending_files)
            if column == 0:
                return f"▶  Load {min(self.FILE_CHUNK, remaining)} more files  ({remaining} remaining) — double-click"
            return ""

        # --- Diff mode nodes ---
        if node.kind == "diff_file":
            if column == 0:
                diff = node.diff_file_data
                count = diff.change_count if diff is not None else len(node.children)
                return f"{node.display_path}    ({count} change(s))"
            return ""  # column 1 is occupied by the apply-file button

        if node.kind == "diff_change":
            # Column 0 text is also used by the delegate for rich rendering.
            # Return a plain-text fallback for copy/accessibility.
            change = node.diff_change
            if change is None:
                return ""
            if column == 0:
                return f"L{change.line_number}  -{change.old_text.rstrip()}  →  {change.new_text.rstrip()}"
            return ""  # column 1 holds the inline apply button

        return ""

    def _tooltip_value(self, node: _ResultNode) -> str:
        if node.kind == "file":
            return node.display_path
        if node.kind == "match" and node.match is not None:
            return f"{node.display_path}:{node.match.line_number}"
        return ""


class ResultPanel(QWidget):
    """Tree-view panel listing files and their match lines.

    Supports two display modes:

    **Normal mode** (default):
        Shows search results grouped by file → match rows.

    **Diff mode** (activated by ``show_diff_preview()``):
        Shows inline replace diffs with colour-coded removed/added lines and
        per-change ``↩`` buttons.  Clicking a button emits
        ``apply_change_requested`` or ``apply_file_diff_requested`` so the
        main window can call ``ReplaceEngine.apply_partial()`` / ``apply()``.
    """

    item_selected = Signal(str, int)          # (file_path, line_number)
    include_rule_requested = Signal(str)
    exclude_rule_requested = Signal(str)
    # Diff-mode apply signals — carry FileDiff / DiffChange objects as `object`
    apply_change_requested = Signal(object, object)   # (FileDiff, DiffChange)
    apply_file_diff_requested = Signal(object)         # FileDiff
    # Preview panel toggle signals — emitted by the bottom title strip
    collapse_preview_requested = Signal()
    uncollapse_preview_requested = Signal()
    # Emitted when the user toggles the "Line wrap" checkbox in the preview bar
    line_wrap_changed = Signal(bool)

    def __init__(self, parent=None, *, lazy_load: bool = False) -> None:
        super().__init__(parent)
        self._lazy_load = lazy_load
        self._last_result: SearchResult | None = None
        self._model = ResultTreeModel(self, lazy_load=lazy_load)
        self._diff_delegate = DiffRowDelegate()
        self._normal_delegate = QStyledItemDelegate()
        # Diff mode state — True when showing replace preview diff
        self._diff_mode: bool = False
        self._diff_diffs: list = []  # list[FileDiff] currently displayed
        # Debounce timer for in-memory fuzzy content filter
        self._filter_timer = QTimer(self)
        self._filter_timer.setSingleShot(True)
        self._filter_timer.setInterval(200)  # 200ms debounce
        self._filter_timer.timeout.connect(self._apply_content_filter)
        # Guard: set column proportions only on the very first show
        self._columns_initialized: bool = False
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        # ── Content filter bar (Mode B — in-memory fuzzy filter) ──────────
        filter_bar = QWidget()
        filter_bar.setFixedHeight(28)
        fb_layout = QHBoxLayout(filter_bar)
        fb_layout.setContentsMargins(4, 2, 4, 2)
        fb_layout.setSpacing(4)

        filter_label = QLabel("Filter:")
        filter_label.setFixedWidth(38)
        fb_layout.addWidget(filter_label)

        self._filter_input = QLineEdit()
        self._filter_input.setPlaceholderText("Fuzzy filter results… (Ctrl+F)")
        self._filter_input.setClearButtonEnabled(True)
        self._filter_input.textChanged.connect(self._on_filter_text_changed)
        fb_layout.addWidget(self._filter_input)

        layout.addWidget(filter_bar)

        # ── Result tree view ───────────────────────────────────────────────
        self._tree = QTreeView()
        self._tree.setModel(self._model)
        self._tree.setUniformRowHeights(True)
        self._tree.setAlternatingRowColors(True)
        self._tree.setRootIsDecorated(True)
        self._tree.setSelectionBehavior(QTreeView.SelectionBehavior.SelectRows)
        self._tree.setSelectionMode(QTreeView.SelectionMode.ExtendedSelection)
        self._tree.setSortingEnabled(False)
        header = self._tree.header()
        # Col 0 (File/Line): Interactive — user drags its right boundary to resize.
        # Col 1 (Match):     auto-fills remaining space (setStretchLastSection).
        # This is the only Qt pattern that allows the divider to be user-draggable
        # while keeping the panel layout proportional.
        header.setStretchLastSection(True)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        self._tree.clicked.connect(self._on_item_clicked)
        self._tree.doubleClicked.connect(self._on_item_double_clicked)
        self._tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._tree.customContextMenuRequested.connect(self._on_context_menu_requested)
        # VS Code-style selection and hover colors:
        #   selected (active focus) → accent blue bg + white text
        #   selected (unfocused)    → muted blue bg + near-black text
        #   hover (not selected)    → light grey bg (not blue like the platform default)
        self._tree.setStyleSheet(
            "QTreeView::item:selected:active   { background-color: #0078D4; color: white; }"
            "QTreeView::item:selected:!active  { background-color: #b4d5fe; color: #1E1E1E; }"
            "QTreeView::item:hover:!selected   { background-color: #E8E8E8; }"
        )
        layout.addWidget(self._tree)

        # ── Preview panel title strip (always visible, even when preview is collapsed)
        separator = QFrame()
        separator.setFrameShape(QFrame.Shape.HLine)
        separator.setFrameShadow(QFrame.Shadow.Sunken)
        layout.addWidget(separator)

        preview_bar = QWidget()
        preview_bar.setFixedHeight(24)
        pb_layout = QHBoxLayout(preview_bar)
        pb_layout.setContentsMargins(4, 0, 4, 0)
        pb_layout.setSpacing(2)

        # "Preview" label on the left
        lbl = QLabel("Preview")
        lbl.setStyleSheet("color: #888888; font-size: 10pt;")
        pb_layout.addWidget(lbl)

        # uncollapse button — uses the same native Qt arrow style as the history dropdown
        btn_up = QToolButton()
        btn_up.setArrowType(Qt.ArrowType.UpArrow)
        btn_up.setFixedSize(20, 20)
        btn_up.setAutoRaise(True)
        btn_up.setToolTip("Expand preview panel")
        btn_up.clicked.connect(self.uncollapse_preview_requested)

        # collapse button
        btn_down = QToolButton()
        btn_down.setArrowType(Qt.ArrowType.DownArrow)
        btn_down.setFixedSize(20, 20)
        btn_down.setAutoRaise(True)
        btn_down.setToolTip("Collapse preview panel")
        btn_down.clicked.connect(self.collapse_preview_requested)

        pb_layout.addWidget(btn_up)
        pb_layout.addWidget(btn_down)
        pb_layout.addStretch()

        # Line-wrap toggle — right side of the preview title strip
        self._line_wrap_cb = QCheckBox("Line wrap")
        self._line_wrap_cb.setChecked(False)  # default: no wrap (preserves horizontal scroll)
        self._line_wrap_cb.setToolTip("Wrap long lines in the preview panel")
        self._line_wrap_cb.setStyleSheet("color: #888888; font-size: 10pt;")
        # toggled(bool) fires on every state change with the new checked value
        self._line_wrap_cb.toggled.connect(self.line_wrap_changed.emit)
        pb_layout.addWidget(self._line_wrap_cb)

        layout.addWidget(preview_bar)

    # ------------------------------------------------------------------
    # Qt event overrides
    # ------------------------------------------------------------------

    def showEvent(self, event) -> None:
        """Set initial 80/20 column proportions on the very first show.

        Using ``showEvent`` (instead of a timer or constructor) guarantees
        the viewport has a real pixel width when we apply the ratio.
        The guard ``_columns_initialized`` ensures subsequent show/hide
        cycles or splitter restores do not override the user's manual
        column adjustments.
        """
        super().showEvent(event)
        if not self._columns_initialized:
            self._columns_initialized = True
            vw = self._tree.viewport().width()
            if vw > 0:
                # Col 0 gets 80 %; col 1 (stretch-last) fills the rest (~20 %).
                self._tree.header().resizeSection(0, max(200, vw * 80 // 100))
                logger.debug("[ResultPanel] Initial column widths: viewport=%d, col0=%d", vw, vw * 80 // 100)

    def clear(self) -> None:
        self._diff_mode = False
        self._diff_diffs = []
        self._tree.setItemDelegateForColumn(0, self._normal_delegate)
        self._tree.setColumnHidden(1, False)
        self._tree.setRootIsDecorated(True)
        self._model.clear()
        self._last_result = None
        self._filter_input.blockSignals(True)
        self._filter_input.clear()
        self._filter_input.blockSignals(False)

    def populate(self, result: SearchResult, *, file_only: bool = False) -> None:
        # Leaving diff mode on new search results
        self._diff_mode = False
        self._diff_diffs = []
        self._tree.setItemDelegateForColumn(0, self._normal_delegate)
        self._last_result = result
        self._model.set_result(result)
        # In Find Files mode: flat list — no tree chrome, no Match column.
        # rootIsDecorated=True reserves branch/expand space for root items even
        # when they have no children; that dead zone swallows clicks, so turn it
        # off when showing a file-only flat list.
        self._tree.setRootIsDecorated(not file_only)
        self._tree.setColumnHidden(1, file_only)
        if not file_only:
            if not self._lazy_load:
                self._tree.expandToDepth(1)
            else:
                self._tree.expandToDepth(0)
        # Clear any stale filter text when new results arrive
        if self._filter_input.text():
            self._filter_input.blockSignals(True)
            self._filter_input.clear()
            self._filter_input.blockSignals(False)

    # ── Diff / replace preview mode ───────────────────────────────────────

    def show_diff_preview(self, diffs: list) -> None:
        """Switch to diff mode: display inline replace diff with per-change buttons.

        Each ``diff_file`` row gets a *Replace File ↩* button (column 1).
        Each ``diff_change`` row gets a small flat *↩* button (column 1).
        Clicking it emits ``apply_change_requested``.

        Diff mode is automatically cleared when ``populate()`` or ``clear()``
        is called (i.e. on next search).
        """
        logger.info("[ResultPanel] Diff mode: %d files", len(diffs))
        self._diff_mode = True
        self._diff_diffs = list(diffs)
        self._last_result = None  # diff and search results are mutually exclusive
        self._model.set_diff(diffs)
        self._tree.setColumnHidden(1, False)
        self._tree.setRootIsDecorated(True)
        self._tree.setItemDelegateForColumn(0, self._diff_delegate)
        self._tree.expandToDepth(1)
        self._install_diff_buttons()

    def _install_diff_buttons(self) -> None:
        """Install *Replace File ↩* and *↩* buttons via ``setIndexWidget``."""
        # Resolve the replace icon once: Codicons TTF (U+EB3D) → PNG fallback →
        # system icon.  All three buttons in this method share the same QIcon.
        apply_icon = _codicon_icon(0xEB3D)  # Codicons 'replace' glyph
        logger.debug("[ResultPanel] Replace icon rendered from Codicons U+EB3D")

        for file_row in range(self._model.rowCount()):
            file_idx = self._model.index(file_row, 0)
            node = self._model.node_for_index(file_idx)
            if node is None or node.kind != "diff_file":
                continue

            diff_data = node.diff_file_data

            # Small apply-file button on column 1 of the file header row
            file_btn = QToolButton()
            file_btn.setIcon(apply_icon)
            file_btn.setFixedSize(22, 22)
            file_btn.setAutoRaise(True)
            file_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
            file_btn.setToolTip(
                f"Apply all {diff_data.change_count} change(s) in {node.file_path.name}"
            )
            file_btn.clicked.connect(
                lambda _checked=False, d=diff_data: self._on_apply_file_diff(d)
            )
            self._tree.setIndexWidget(self._model.index(file_row, 1), file_btn)

            # Small apply button on column 1 of each diff_change row
            for child_row in range(self._model.rowCount(file_idx)):
                child_idx = self._model.index(child_row, 0, file_idx)
                child_node = self._model.node_for_index(child_idx)
                if child_node is None or child_node.kind != "diff_change":
                    continue

                change = child_node.diff_change
                change_btn = QToolButton()
                change_btn.setIcon(apply_icon)
                change_btn.setFixedSize(22, 22)
                change_btn.setAutoRaise(True)
                change_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
                change_btn.setToolTip(f"Apply this change (line {change.line_number})")
                change_btn.clicked.connect(
                    lambda _checked=False, d=diff_data, c=change: self._on_apply_single_change(d, c)
                )
                self._tree.setIndexWidget(
                    self._model.index(child_row, 1, file_idx), change_btn
                )

    def _on_apply_file_diff(self, diff_data: object) -> None:
        """Emit signal requesting the main window apply all changes in one file."""
        self.apply_file_diff_requested.emit(diff_data)

    def _on_apply_single_change(self, diff_data: object, change: object) -> None:
        """Emit signal requesting the main window apply one line change."""
        self.apply_change_requested.emit(diff_data, change)

    def remove_diff_file(self, file_path: Path) -> None:
        """Remove a diff_file node after the file's changes have been applied."""
        self._model.remove_diff_file(file_path)

    def remove_diff_change(self, file_path: Path, change_line: int) -> None:
        """Remove a ``diff_change`` node after a single change is applied."""
        self._model.remove_diff_change(file_path, change_line)

    def mark_change_applied(self, file_path: Path, change_line: int) -> None:
        """Mark a single ``diff_change`` node as applied.

        Hides the ↩ button (column 1) and re-renders the row in the green
        "applied" style (✓ + new text).  The node stays in the tree.
        """
        for file_row, file_node in enumerate(self._model._root.children):
            if file_node.kind != "diff_file" or file_node.file_path != file_path:
                continue
            file_idx = self._model.index(file_row, 0)
            for child_row, child_node in enumerate(file_node.children):
                if child_node.kind != "diff_change":
                    continue
                change = child_node.diff_change
                if change is None or change.line_number != change_line:
                    continue
                child_node.applied = True
                # Hide the ↩ button in column 1.
                col1_idx = self._model.index(child_row, 1, file_idx)
                self._tree.setIndexWidget(col1_idx, None)
                # Trigger delegate repaint on column 0.
                col0_idx = self._model.index(child_row, 0, file_idx)
                self._model.dataChanged.emit(col0_idx, col1_idx)
            # Force an immediate viewport repaint so the applied style is
            # visible without needing an extra mouse move/click.
            self._tree.viewport().update()
            break

    def mark_file_applied(self, file_path: Path) -> None:
        """Mark all ``diff_change`` children of a ``diff_file`` node as applied.

        Hides the file-level and all per-line ↩ buttons; re-renders every
        change row in the green "applied" style.  Nodes stay in the tree.
        """
        for file_row, file_node in enumerate(self._model._root.children):
            if file_node.kind != "diff_file" or file_node.file_path != file_path:
                continue
            file_idx = self._model.index(file_row, 0)
            # Hide the file-level ↩ button.
            file_col1_idx = self._model.index(file_row, 1)
            self._tree.setIndexWidget(file_col1_idx, None)
            # Mark each change row.
            for child_row, child_node in enumerate(file_node.children):
                if child_node.kind != "diff_change":
                    continue
                child_node.applied = True
                col1_idx = self._model.index(child_row, 1, file_idx)
                self._tree.setIndexWidget(col1_idx, None)
                col0_idx = self._model.index(child_row, 0, file_idx)
                self._model.dataChanged.emit(col0_idx, col1_idx)
            self._tree.viewport().update()
            break

    # ── Content filter (Mode B — 200ms debounce in-memory fuzzy) ──────────

    def focus_filter(self) -> None:
        """Focus the content filter input (keyboard shortcut Ctrl+F)."""
        self._filter_input.setFocus()
        self._filter_input.selectAll()

    def _on_filter_text_changed(self, _text: str) -> None:
        """Restart debounce timer on every keystroke."""
        self._filter_timer.start()

    def _apply_content_filter(self) -> None:
        """Apply the current filter text to the loaded result set."""
        query = self._filter_input.text().strip()
        if not query:
            # Empty filter — restore full result
            if self._last_result is not None:
                self._model.set_result(self._last_result)
                if not self._lazy_load:
                    self._tree.expandToDepth(1)
            return

        if self._last_result is None:
            return

        filtered = _filter_result(query, self._last_result)
        self._model.set_result(filtered)
        self._tree.expandToDepth(1)

    def current_result(self) -> SearchResult | None:
        """Return the most recently populated SearchResult, or None."""
        return self._last_result

    def load_from_snapshot(self, snapshot: "ResultSnapshot") -> None:  # type: ignore[name-defined]
        """Reconstruct a SearchResult from a ResultSnapshot and populate the panel.

        The stored file_filter rules are applied in memory so that files which
        were saved despite matching an exclude rule (e.g. because the rule was
        added after the search ran) are hidden immediately on load.
        """
        from osprey.results.io import to_search_result  # lazy import to keep Qt-free
        result = to_search_result(snapshot)
        # Apply the stored include/exclude rules to the reconstructed file list.
        filtered = result.query.file_filter.filter_result(result)
        self.populate(filtered)

    def _on_item_clicked(self, index: QModelIndex) -> None:
        node = self._model.node_for_index(index)
        if node is None or node.file_path is None:
            return
        line_number = node.line_number
        self.item_selected.emit(str(node.file_path), int(line_number))

    def _on_item_double_clicked(self, index: QModelIndex) -> None:
        if self._model.is_more_sentinel(index):
            added = self._model.load_more_files()
            if added:
                self._tree.scrollTo(index)

    def _on_context_menu_requested(self, position) -> None:
        right_click_idx = self._tree.indexAt(position)

        # Collect selected row indices (column 0 only — one per row)
        selected_col0 = {
            idx.siblingAtColumn(0)
            for idx in self._tree.selectedIndexes()
            if idx.isValid()
        }

        # If right-click lands outside current selection, scope to that item only
        rc_col0 = right_click_idx.siblingAtColumn(0) if right_click_idx.isValid() else None
        if rc_col0 is None:
            return
        if rc_col0 not in selected_col0:
            selected_col0 = {rc_col0}

        # Gather unique file paths from selected nodes
        file_paths: list[Path] = []
        seen_paths: set[str] = set()
        for idx in selected_col0:
            node = self._model.node_for_index(idx)
            if node is not None and node.file_path is not None:
                key = node.file_path.as_posix()
                if key not in seen_paths:
                    seen_paths.add(key)
                    file_paths.append(node.file_path)

        if not file_paths:
            return

        file_paths.sort(key=lambda p: p.as_posix())
        # Deduplicate parent directories
        parent_dirs: list[Path] = sorted(
            {p.parent for p in file_paths},
            key=lambda d: d.as_posix(),
        )
        n_files = len(file_paths)
        n_dirs = len(parent_dirs)

        # Build context menu with count suffixes when multi-selection is active
        menu = QMenu(self)
        inc_file_lbl  = f"Include files ({n_files})" if n_files > 1 else "Include file"
        exc_file_lbl  = f"Exclude files ({n_files})" if n_files > 1 else "Exclude file"
        dir_noun = "directories" if n_dirs > 1 else "directory"
        inc_dir_lbl = (
            f"Include parent {dir_noun} ({n_dirs})" if n_dirs > 1
            else "Include parent directory"
        )
        exc_dir_lbl = (
            f"Exclude parent {dir_noun} ({n_dirs})" if n_dirs > 1
            else "Exclude parent directory"
        )

        include_file_action = menu.addAction(inc_file_lbl)
        exclude_file_action = menu.addAction(exc_file_lbl)
        menu.addSeparator()
        include_dir_action = menu.addAction(inc_dir_lbl)
        exclude_dir_action = menu.addAction(exc_dir_lbl)

        chosen = menu.exec(self._tree.viewport().mapToGlobal(position))

        if chosen == include_file_action:
            for p in file_paths:
                # Use str() not as_posix() so Windows emits native backslash paths;
                # mixed-slash (POSIX rule vs backslash search path) causes rg 0 matches.
                self.include_rule_requested.emit(str(p))
        elif chosen == exclude_file_action:
            for p in file_paths:
                self.exclude_rule_requested.emit(str(p))
        elif chosen == include_dir_action:
            for d in parent_dirs:
                self.include_rule_requested.emit(str(d))
        elif chosen == exclude_dir_action:
            for d in parent_dirs:
                self.exclude_rule_requested.emit(str(d))
