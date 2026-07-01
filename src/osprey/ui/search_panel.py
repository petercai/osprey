"""
osprey.ui.search_panel
~~~~~~~~~~~~~~~~~~~~~~
Compact top panel: shared common bar + tabbed area.

Layout (top-to-bottom):
  Common bar  : Search pattern (3-row QTextEdit) + regex/case/word toggle buttons
                Directory picker + engine selector (single compact row)
  QTabWidget  : Search | Replace | Find Files
    Search    : ChipFilterBar for include/exclude (stacked, compact)
    Replace   : Replacement text input + Preview button
    Find Files: File-only mode
  Action row  : Search / browse buttons (always visible)
"""

from __future__ import annotations

import json
import logging

from pathlib import Path

logger = logging.getLogger(__name__)

from PySide6.QtCore import Qt, QEvent, QPoint, QRect, QSize, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTabWidget,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from osprey.config.settings import AppSettings, SearchProfile
from osprey.engine.base import FileFilter, SearchQuery


# ---------------------------------------------------------------------------
# _RuleChip — one removable chip widget
# ---------------------------------------------------------------------------

class _RuleChip(QFrame):
    """Editable, removable token chip.

    Display mode  : [rule text (click to edit)] [\u00d7]
    Edit mode     : [QLineEdit with current text] [\u00d7]

    Signals
    -------
    remove_requested(rule: str)            \u2014 \u00d7 button clicked or empty commit
    edit_committed(old: str, new: str)     \u2014 text changed and committed
    """

    remove_requested = Signal(str)
    edit_committed = Signal(str, str)  # (old_rule, new_rule)

    def __init__(self, text: str, parent=None) -> None:
        super().__init__(parent)
        self._text = text
        self.setFrameShape(QFrame.Shape.StyledPanel)
        # Palette-relative colors \u2014 adapts to light and dark themes
        self.setStyleSheet(
            "QFrame{background:palette(button);border:1px solid palette(mid);"
            "border-radius:3px;padding:0}"
        )
        row = QHBoxLayout(self)
        row.setContentsMargins(2, 1, 2, 1)
        row.setSpacing(2)

        # --- Display mode: label with IBeam cursor to hint editability ---
        self._text_lbl = QLabel(text)
        self._text_lbl.setStyleSheet(
            "QLabel{border:none;background:transparent;font-size:11px;padding:0 2px}"
        )
        self._text_lbl.setCursor(Qt.CursorShape.IBeamCursor)
        self._text_lbl.setToolTip("Click to edit")
        # Override instance method to enter edit mode on click
        self._text_lbl.mousePressEvent = lambda _e: self._enter_edit_mode()
        row.addWidget(self._text_lbl)

        # --- Edit mode: inline QLineEdit (hidden initially) ---
        self._edit_input = QLineEdit(text)
        self._edit_input.setFixedHeight(20)
        self._edit_input.setStyleSheet(
            "QLineEdit{border:1px solid palette(highlight);border-radius:2px;"
            "font-size:11px;padding:0 2px;background:palette(base)}"
        )
        self._edit_input.hide()
        self._edit_input.returnPressed.connect(self._commit_edit)
        self._edit_input.installEventFilter(self)
        row.addWidget(self._edit_input)

        # --- Delete button (always visible in both modes) ---
        del_btn = QToolButton()
        del_btn.setText("\u00d7")
        del_btn.setFixedSize(14, 14)
        del_btn.setStyleSheet(
            "QToolButton{border:none;color:#aaa;background:transparent;font-size:10px}"
            "QToolButton:hover{color:#f66}"
        )
        del_btn.setToolTip("Remove rule")
        del_btn.clicked.connect(lambda: self.remove_requested.emit(self._text))
        row.addWidget(del_btn)

    def eventFilter(self, obj, event) -> bool:
        """Intercept Escape (cancel) and Tab (commit) on the inline edit input."""
        if obj is self._edit_input and event.type() == QEvent.Type.KeyPress:
            key = event.key()
            if key == Qt.Key.Key_Escape:
                self._cancel_edit()
                return True
            if key in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab):
                self._commit_edit()
                return True
        return super().eventFilter(obj, event)

    def _enter_edit_mode(self) -> None:
        self._text_lbl.hide()
        self._edit_input.setText(self._text)
        # Size the input field to comfortably contain the current text
        fm = self._edit_input.fontMetrics()
        self._edit_input.setMinimumWidth(max(60, fm.horizontalAdvance(self._text) + 24))
        self._edit_input.show()
        self._edit_input.setFocus()
        self._edit_input.selectAll()

    def _commit_edit(self) -> None:
        if self._edit_input.isHidden():  # not in edit mode — nothing to commit
            return
        new_text = self._edit_input.text().strip()
        self._edit_input.hide()
        self._text_lbl.show()
        if not new_text:
            # Empty commit \u2014 treat as delete
            self.remove_requested.emit(self._text)
        elif new_text != self._text:
            self.edit_committed.emit(self._text, new_text)

    def _cancel_edit(self) -> None:
        self._edit_input.hide()
        self._text_lbl.show()

    @property
    def rule(self) -> str:
        return self._text


# ---------------------------------------------------------------------------
# ChipFilterBar — VS Code-style wrapping flow chip editor
# ---------------------------------------------------------------------------

class _FlowLayout(QLayout):
    """Left-to-right wrapping flow layout (Qt has no built-in equivalent).

    Items are placed in a row; when a new item would exceed the available
    width a new row begins.  The layout reports ``hasHeightForWidth = True``
    so parent widgets can correctly compute the required height for a given
    width, which makes it work properly inside a ``QScrollArea``.
    """

    def __init__(self, h_spacing: int = 4, v_spacing: int = 3, parent=None) -> None:
        super().__init__(parent)
        self._items: list = []
        self._h_spacing = h_spacing
        self._v_spacing = v_spacing

    def addItem(self, item) -> None:
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int):
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    def takeAt(self, index: int):
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._do_layout(QRect(0, 0, width, 0), test_only=True)

    def setGeometry(self, rect: QRect) -> None:
        super().setGeometry(rect)
        self._do_layout(rect, test_only=False)

    def sizeHint(self) -> QSize:
        return self.minimumSize()

    def minimumSize(self) -> QSize:
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        m = self.contentsMargins()
        size += QSize(m.left() + m.right(), m.top() + m.bottom())
        return size

    def _do_layout(self, rect: QRect, test_only: bool) -> int:
        """Arrange items row-by-row; return total height used."""
        m = self.contentsMargins()
        r = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        x, y = r.x(), r.y()
        row_h = 0

        for item in self._items:
            hint = item.sizeHint()
            next_x = x + hint.width() + self._h_spacing
            if x > r.x() and next_x - self._h_spacing > r.right():
                # Wrap to next row
                x = r.x()
                y += row_h + self._v_spacing
                next_x = x + hint.width() + self._h_spacing
                row_h = 0
            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x = next_x
            row_h = max(row_h, hint.height())

        return y + row_h - r.y() + m.top() + m.bottom()


class _FlowContainer(QWidget):
    """QWidget whose sizeHint height is derived from its FlowLayout.

    Required so QScrollArea.setWidgetResizable(True) correctly resizes
    the container width and then asks for the matching content height.
    """

    def sizeHint(self) -> QSize:
        lo = self.layout()
        if lo and lo.hasHeightForWidth():
            w = max(self.width(), 80)
            h = lo.heightForWidth(w)
            return QSize(w, h)
        return super().sizeHint()

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, w: int) -> int:
        lo = self.layout()
        if lo:
            return lo.heightForWidth(w)
        return super().heightForWidth(w)


class ChipFilterBar(QWidget):
    """VS Code-style wrapping flow chip editor.

    Chips are arranged via a custom FlowLayout that wraps to additional rows
    when horizontal space is exhausted.  A QLineEdit at the end of the flow
    accepts new rules on Enter (Return key).  Bounded to three rows (~82 px);
    a vertical scrollbar appears when more rules overflow.

    Public API
    ----------
    rules()         -> list[str]
    set_rules(...)  -> None
    add_rule(...)   -> bool
    rules_changed   -> Signal(list)
    """

    rules_changed = Signal(list)

    def __init__(self, label: str, placeholder: str, parent=None) -> None:
        super().__init__(parent)
        self._rules: list[str] = []
        self._chips: list[_RuleChip] = []

        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(4)

        lbl = QLabel(label + ":")
        lbl.setFixedWidth(54)
        # Align label to top-right so it lines up with the first chip row
        lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
        lbl.setContentsMargins(0, 6, 0, 0)
        outer.addWidget(lbl)

        # Flow container — chips + add-input all live in _FlowLayout
        self._container = _FlowContainer()
        self._flow = _FlowLayout(h_spacing=4, v_spacing=3)
        self._flow.setContentsMargins(3, 3, 3, 3)
        self._container.setLayout(self._flow)

        # Ghost input: invisible until focused, sits after the last chip
        self._add_input = QLineEdit()
        self._add_input.setPlaceholderText(placeholder)
        self._add_input.setMinimumWidth(80)
        self._add_input.setMaximumWidth(180)
        self._add_input.setFixedHeight(24)
        self._add_input.setStyleSheet(
            "QLineEdit{border:none;background:transparent;font-size:11px}"
        )
        self._add_input.returnPressed.connect(self._on_add_pressed)
        self._flow.addWidget(self._add_input)

        # Scroll height formula: 34 + (N-1) * 24 where N = chip rows
        #   step = rendered chip height (≈21 px) + v_gap (3 px) = 24 px/row
        #   1 row → 34 px   2 rows → 58 px   3 rows → 82 px
        self._scroll = QScrollArea()
        self._scroll.setWidget(self._container)
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._scroll.setMinimumHeight(34)   # 1 chip row
        self._scroll.setMaximumHeight(58)   # 2 chip rows
        self._scroll.setFrameShape(QFrame.Shape.StyledPanel)
        self._scroll.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

        outer.addWidget(self._scroll, 1)

    # ------------------------------------------------------------------
    def _on_add_pressed(self) -> None:
        rule = self._add_input.text().strip()
        if rule and rule not in self._rules:
            self._rules.append(rule)
            self._add_input.clear()
            self._rebuild()
            self.rules_changed.emit(self._rules)

    def _remove_rule(self, rule: str) -> None:
        if rule in self._rules:
            self._rules.remove(rule)
            self._rebuild()
            self.rules_changed.emit(self._rules)

    def _on_chip_edit(self, old_rule: str, new_rule: str) -> None:
        """Replace a rule after in-place token edit; silently rejects duplicates."""
        if new_rule in self._rules and new_rule != old_rule:
            return  # duplicate \u2014 chip will visually revert on next rebuild
        try:
            idx = self._rules.index(old_rule)
        except ValueError:
            return
        self._rules[idx] = new_rule
        self._rebuild()
        self.rules_changed.emit(self._rules)

    def _rebuild(self) -> None:
        """Rebuild the flow: remove all chips, re-insert in order, keep add-input last."""
        logger.debug("[ChipFilterBar:%x] _rebuild start — %d rule(s): %s", id(self), len(self._rules), self._rules)
        # Detach and schedule deletion of all existing chips
        for chip in self._chips:
            self._flow.removeWidget(chip)
            chip.hide()
            chip.setParent(None)
            chip.deleteLater()
        self._chips.clear()

        # Temporarily remove add-input so we can insert chips before it
        self._flow.removeWidget(self._add_input)

        # Re-insert chips then add-input
        for rule in self._rules:
            chip = _RuleChip(rule, self._container)
            chip.remove_requested.connect(self._remove_rule)
            chip.edit_committed.connect(self._on_chip_edit)  # inline edit support
            self._flow.addWidget(chip)
            chip.show()
            self._chips.append(chip)
            logger.debug("[ChipFilterBar:%x] chip created: %r  visible=%s", id(self), rule, chip.isVisible())

        self._flow.addWidget(self._add_input)
        # Notify the container and scroll area to re-compute geometry
        self._container.updateGeometry()
        self._container.update()
        logger.debug("[ChipFilterBar:%x] _rebuild done — %d chip(s) in flow", id(self), len(self._chips))

    # Public API -------------------------------------------------------

    def rules(self) -> list[str]:
        return list(self._rules)

    def set_rules(self, rules: list[str]) -> None:
        self._rules = [r.strip() for r in rules if r.strip()]
        logger.debug("[ChipFilterBar:%x] set_rules called → %d rule(s): %s", id(self), len(self._rules), self._rules)
        self._rebuild()
        self.rules_changed.emit(self._rules)

    def add_rule(self, rule: str) -> bool:
        """Add rule if not already present. Returns True when added."""
        cleaned = rule.strip()
        if cleaned and cleaned not in self._rules:
            self._rules.append(cleaned)
            self._rebuild()
            self.rules_changed.emit(self._rules)
            return True
        return False


# ---------------------------------------------------------------------------
# SearchPanel
# ---------------------------------------------------------------------------

class SearchPanel(QWidget):
    """Compact top configuration panel with tabbed sub-areas.

    Signals
    -------
    search_requested           : user triggered a search
    replace_preview_requested  : (pattern, replacement) replace preview
    filter_changed             : include/exclude chip rules changed
    """

    search_requested = Signal()
    replace_preview_requested = Signal(str, str)   # (pattern, replacement) → inline diff
    replace_all_requested = Signal(str, str)        # (pattern, replacement) → direct apply
    # Emitted whenever include or exclude rules are added/removed/edited in the chip
    # bars; main_window connects this to re-filter the current result in memory.
    filter_changed = Signal()

    def __init__(
        self,
        available_engines: list[str],
        settings: AppSettings,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        # Tracks whether the next search_requested should use file-only mode.
        # Set True by _on_findfiles(), reset to False immediately after get_query().
        self._file_only_mode: bool = False
        self._build_ui(available_engines)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self, engines: list[str]) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 2)
        root.setSpacing(4)

        root.addLayout(self._build_query_row())
        root.addLayout(self._build_dir_engine_row(engines))

        self._tabs = QTabWidget()
        self._tabs.setDocumentMode(True)
        self._tabs.addTab(self._build_search_tab(), "Search")
        self._tabs.addTab(self._build_replace_tab(), "Replace")
        self._tabs.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        root.addWidget(self._tabs)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)

    def _build_query_row(self) -> QHBoxLayout:
        """History dropdown (acts as Search: label) + 3-line QTextEdit (60 %) + R/Aa/\\b toggle buttons."""
        row = QHBoxLayout()
        row.setSpacing(4)

        # History dropdown — replaces the static "Search:" label.
        # Clicking opens a popup menu with recent search patterns.
        self._history_btn = QToolButton()
        self._history_btn.setText("Search:")
        self._history_btn.setToolTip("Recent patterns — click to fill from history")
        self._history_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self._history_btn.setFixedWidth(58)
        self._history_btn.setContentsMargins(0, 4, 0, 0)
        self._history_btn.setStyleSheet(
            "QToolButton{font-size:11px;border:none;background:transparent;"
            "padding-right:4px;text-align:right}"
        )
        self._history_menu = QMenu(self._history_btn)
        self._history_btn.setMenu(self._history_menu)
        self._rebuild_history_menu()
        row.addWidget(self._history_btn, 0, Qt.AlignmentFlag.AlignTop)

        self._pattern_input = QTextEdit()
        self._pattern_input.setPlaceholderText("Pattern…")
        self._pattern_input.setAcceptRichText(False)
        self._pattern_input.setTabChangesFocus(True)
        fm = self._pattern_input.fontMetrics()
        self._pattern_input.setFixedHeight(fm.lineSpacing() * 2 + 12)
        self._pattern_input.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        # Pattern is intentionally NOT restored from search_history on startup.
        # Plain startup always begins with an empty pattern; history remains available
        # for completion/dropdowns. Only explicit .opq profile loading pre-fills the pattern.
        row.addWidget(self._pattern_input, 6)  # ~60 % stretch factor

        btn_col = QHBoxLayout()
        btn_col.setSpacing(2)
        btn_col.setContentsMargins(0, 0, 0, 0)
        self._regex_btn = self._toggle_btn(".*", "Regular expression")
        self._case_btn = self._toggle_btn("Aa", "Case sensitive")
        self._case_btn.setChecked(True)
        self._word_btn = self._toggle_btn("\\b", "Whole word")
        btn_col.addWidget(self._case_btn)
        btn_col.addWidget(self._word_btn)
        btn_col.addWidget(self._regex_btn)
        row.addLayout(btn_col)
        row.addStretch(1)  # remaining ~30 % (contains engine row on same level)
        return row

    @staticmethod
    def _toggle_btn(label: str, tooltip: str) -> QPushButton:
        btn = QPushButton(label)
        btn.setCheckable(True)
        btn.setFixedSize(28, 20)
        btn.setToolTip(tooltip)
        btn.setStyleSheet(
            "QPushButton{font-size:10px;font-family:monospace;"
            "border:1px solid #666;border-radius:2px}"
            "QPushButton:checked{background:#0e639c;color:white;border-color:#0e639c}"
        )
        return btn

    def _build_dir_engine_row(self, engines: list[str]) -> QHBoxLayout:
        """Single row: Dir chip bar (70 %) + browse btn + engine dropdown (30 %).

        Engine label removed — tooltip identifies the combo.
        Dir bar minimum height raised to ~2 chip rows (~60 px).
        """
        row = QHBoxLayout()
        row.setSpacing(4)
        row.setContentsMargins(0, 0, 0, 0)

        # Multi-dir chip bar — stretch factor 7 gives it ~70 % of flexible width
        self._dir_bar = ChipFilterBar("Folders", "path/to/dir…")
        init_dirs = [self._settings.last_directory] if self._settings.last_directory else []
        self._dir_bar.set_rules(init_dirs)
        # Keep last_directory in sync with the first chip in the bar
        self._dir_bar.rules_changed.connect(self._on_dirs_changed)
        # Two chip rows: row_h≈26 px × 1 + v_gap≈3 + frame≈4 ≈ 35 px
        self._dir_bar._scroll.setMinimumHeight(35)
        row.addWidget(self._dir_bar, 7)

        # Browse btn — adds dir to list rather than replacing
        browse_btn = QPushButton("+…")
        browse_btn.setFixedWidth(32)
        browse_btn.setToolTip("Browse and add a search directory to the list")
        browse_btn.clicked.connect(self._browse_directory)
        row.addWidget(browse_btn)

        # Engine dropdown — no label; stretch 3 keeps it at ~30 % of flexible width
        self._engine_combo = QComboBox()
        for e in engines:
            self._engine_combo.addItem(e)
        idx = self._engine_combo.findText(self._settings.engine_preference)
        if idx >= 0:
            self._engine_combo.setCurrentIndex(idx)
        self._engine_combo.setMinimumWidth(70)
        self._engine_combo.setMaximumWidth(130)
        self._engine_combo.setToolTip("Search engine")
        row.addWidget(self._engine_combo, 3)

        return row

    def _build_search_tab(self) -> QWidget:
        """Search tab: [Search / Find Files buttons] | [filter controls]."""
        tab = QWidget()
        tab.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        outer = QHBoxLayout(tab)
        outer.setContentsMargins(4, 4, 4, 2)
        outer.setSpacing(6)

        # Left column: Search + Find Files action buttons
        search_btn_grp = QVBoxLayout()
        search_btn_grp.setSpacing(4)
        search_btn_grp.setContentsMargins(0, 0, 0, 0)

        self._search_btn = QPushButton("Search")
        self._search_btn.setFixedSize(78, 28)
        self._search_btn.setStyleSheet("QPushButton{font-weight:bold}")
        self._search_btn.clicked.connect(self._on_search_clicked)
        search_btn_grp.addWidget(self._search_btn)

        find_files_btn = QPushButton("Find Files")
        find_files_btn.setFixedSize(78, 28)
        find_files_btn.setToolTip("Search for matching file names only (no content match)")
        find_files_btn.clicked.connect(self._on_findfiles)
        search_btn_grp.addWidget(find_files_btn)

        search_btn_grp.addStretch()
        outer.addLayout(search_btn_grp)

        # Right column: filter controls
        search_filter_grp = QHBoxLayout()
        search_filter_grp.setSpacing(3)
        search_filter_grp.setContentsMargins(0, 0, 0, 0)

        self._include_bar = ChipFilterBar("Include", "*.py  src/**")
        # Include/exclude rules are intentionally NOT restored from global settings on startup.
        # Rules are empty on fresh start; only explicit .opq profile loading pre-fills them.
        self._include_bar.rules_changed.connect(self._on_include_changed)
        search_filter_grp.addWidget(self._include_bar, 0, Qt.AlignmentFlag.AlignTop)  # stretch factor 0 keeps it ~same width as exclude bar

        self._exclude_bar = ChipFilterBar("Exclude", "*.min.js  dist/")
        self._exclude_bar.rules_changed.connect(self._on_exclude_changed)
        search_filter_grp.addWidget(self._exclude_bar, 0, Qt.AlignmentFlag.AlignTop)  # stretch factor 1 keeps it ~same width as include bar

        self._filter_regex_check = QCheckBox("regex filter")
        self._filter_regex_check.setChecked(False)
        search_filter_grp.addWidget(self._filter_regex_check, 0, Qt.AlignmentFlag.AlignTop)

        search_filter_grp.addStretch()
        outer.addLayout(search_filter_grp, 1)

        return tab

    def _build_replace_tab(self) -> QWidget:
        """Replace tab: 3-line replacement input (60 %) + Preview / Replace All buttons.

        Layout mirrors the search query row:
          [Replace: label]  [3-row QTextEdit, ~60%]  [Preview]   (stretch)
                                                      [Replace All]
        """
        tab = QWidget()
        tab.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        outer = QHBoxLayout(tab)
        outer.setContentsMargins(4, 4, 4, 2)
        outer.setSpacing(4)

        lbl = QLabel("Replace:")
        lbl.setFixedWidth(58)
        lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        # AlignTop as addWidget's third arg pins the widget to the top of the row
        # without stretching it, so the label's natural height (~1 line) stays at y=0.
        outer.addWidget(lbl, 0, Qt.AlignmentFlag.AlignTop)

        # 3-line QTextEdit for the replacement text (~60 % width via stretch)
        self._replace_input = QTextEdit()
        self._replace_input.setPlaceholderText("Replacement text…")
        self._replace_input.setAcceptRichText(False)
        self._replace_input.setTabChangesFocus(True)
        fm = self._replace_input.fontMetrics()
        self._replace_input.setFixedHeight(fm.lineSpacing() * 2 + 12)
        self._replace_input.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        outer.addWidget(self._replace_input, 6, Qt.AlignmentFlag.AlignTop)

        # Two-button column: Preview (top) + Replace All (bottom)
        replace_button_grp = QHBoxLayout()
        replace_button_grp.setSpacing(4)
        replace_button_grp.setContentsMargins(0, 0, 0, 0)

        preview_btn = QPushButton("Preview")
        preview_btn.setToolTip("Show inline diff of all replacements in the result panel")
        preview_btn.clicked.connect(self._emit_replace_preview)
        replace_button_grp.addWidget(preview_btn)

        replace_all_btn = QPushButton("Replace")
        replace_all_btn.setToolTip("Apply replacements to all matched files immediately")
        replace_all_btn.clicked.connect(self._emit_replace_all)
        replace_button_grp.addWidget(replace_all_btn)

        # Backup toggle — sits directly below Replace All.
        # Default: unchecked (no backup).  When checked, each apply call
        # writes a *.osprey.bak copy before overwriting the original file.
        self._backup_check = QCheckBox("Backup")
        self._backup_check.setChecked(False)
        self._backup_check.setToolTip(
            "Create a *.osprey.bak backup file before each replace operation"
        )
        replace_button_grp.addWidget(self._backup_check)

        replace_button_grp.addStretch()
        outer.addLayout(replace_button_grp)
        outer.setAlignment(replace_button_grp, Qt.AlignmentFlag.AlignTop)
        outer.addStretch(1)  # absorb remaining ~30 %
        return tab

    def _rebuild_history_menu(self) -> None:
        """Rebuild the history dropdown from AppSettings.search_history."""
        self._history_menu.clear()
        history = self._settings.search_history[:20]  # show at most 20 entries
        if not history:
            no_item = self._history_menu.addAction("(no history)")
            no_item.setEnabled(False)
        else:
            for pattern in history:
                action = self._history_menu.addAction(pattern[:80])
                action.setData(pattern)
                action.triggered.connect(self._on_history_selected)

    def _on_history_selected(self) -> None:
        action = self.sender()
        if action is None:
            return
        pattern = action.data()
        if pattern:
            self._pattern_input.setPlainText(pattern)

    # ------------------------------------------------------------------
    # Internal slots
    # ------------------------------------------------------------------

    def _browse_directory(self) -> None:
        """Open directory picker; adds the chosen path to the dir chip bar."""
        dirs = self._dir_bar.rules()
        start = dirs[0] if dirs else self._settings.last_directory
        chosen = QFileDialog.getExistingDirectory(self, "Select Search Directory", start)
        if chosen:
            self._dir_bar.add_rule(chosen)

    def _on_dirs_changed(self, dirs: list) -> None:
        """Sync last_directory setting with the first dir chip."""
        if dirs:
            self._settings.last_directory = dirs[0]

    def _on_search_clicked(self) -> None:
        self._file_only_mode = False  # normal content search
        self._tabs.setCurrentIndex(0)
        self.search_requested.emit()
        # Refresh history dropdown after each search (main_window updates history)
        self._rebuild_history_menu()

    @property
    def is_find_files_mode(self) -> bool:
        """True while a Find Files search is in progress (reset after emit)."""
        return self._file_only_mode

    def _on_findfiles(self) -> None:
        """Trigger a file-names-only search; resets file_only_mode after query is captured."""
        self._file_only_mode = True
        self.search_requested.emit()  # get_query() called synchronously before this returns
        self._file_only_mode = False  # reset so next plain Search click is not file-only

    def _emit_replace_preview(self) -> None:
        pattern = self._pattern_text().strip()
        replacement = self._replace_input.toPlainText()
        if pattern:
            self.replace_preview_requested.emit(pattern, replacement)

    def _emit_replace_all(self) -> None:
        pattern = self._pattern_text().strip()
        replacement = self._replace_input.toPlainText()
        if pattern:
            self.replace_all_requested.emit(pattern, replacement)

    def replacement_text(self) -> str:
        """Return the current replacement string."""
        return self._replace_input.toPlainText()

    @property
    def backup_enabled(self) -> bool:
        """Return ``True`` when the user has opted in to pre-replace backups."""
        return self._backup_check.isChecked()

    def _pattern_text(self) -> str:
        """Return the first non-empty line of the multi-line pattern input."""
        for line in self._pattern_input.toPlainText().splitlines():
            if line.strip():
                return line.strip()
        return self._pattern_input.toPlainText().strip()

    def _on_include_changed(self, rules: list[str]) -> None:
        self._settings.include_rules = list(rules)
        for rule in rules:
            self._settings.touch_recent_rule(rule, kind="include")
        self.filter_changed.emit()

    def _on_exclude_changed(self, rules: list[str]) -> None:
        self._settings.exclude_rules = list(rules)
        for rule in rules:
            self._settings.touch_recent_rule(rule, kind="exclude")
        self.filter_changed.emit()

    # ------------------------------------------------------------------
    # Public API (interface unchanged — MainWindow compatibility)
    # ------------------------------------------------------------------

    def directories(self) -> list[str]:
        """Return the current list of search directories from the dir chip bar."""
        return self._dir_bar.rules()

    def set_directory(self, path: str) -> None:
        """Replace the dir list with a single directory (external callers)."""
        if path:
            self._dir_bar.set_rules([path])

    def set_directories(self, paths: list[str]) -> None:
        """Replace the directory list with all given paths (e.g. from .opr load)."""
        valid = [p.strip() for p in paths if p.strip()]
        if valid:
            self._dir_bar.set_rules(valid)

    def set_pattern(self, pattern: str) -> None:
        """Pre-fill the search pattern text field."""
        self._pattern_input.setPlainText(pattern)

    def set_regex(self, value: bool) -> None:
        """Toggle the regex search mode button."""
        self._regex_btn.setChecked(value)

    def set_case_sensitive(self, value: bool) -> None:
        """Toggle the case-sensitive search mode button."""
        self._case_btn.setChecked(value)

    def set_whole_word(self, value: bool) -> None:
        """Toggle the whole-word search mode button."""
        self._word_btn.setChecked(value)

    def set_include_rules(self, rules: list[str]) -> None:
        self._include_bar.set_rules(rules)
        self._settings.include_rules = self._include_bar.rules()

    def set_exclude_rules(self, rules: list[str]) -> None:
        self._exclude_bar.set_rules(rules)
        self._settings.exclude_rules = self._exclude_bar.rules()

    def add_include_rule(self, rule: str) -> None:
        """Append one include rule if it does not already exist."""
        if self._include_bar.add_rule(rule):
            self._settings.include_rules = self._include_bar.rules()

    def add_exclude_rule(self, rule: str) -> None:
        """Append one exclude rule if it does not already exist."""
        if self._exclude_bar.add_rule(rule):
            self._settings.exclude_rules = self._exclude_bar.rules()

    def set_engine_preference(self, engine_name: str) -> None:
        idx = self._engine_combo.findText(engine_name)
        if idx >= 0:
            self._engine_combo.setCurrentIndex(idx)

    def engine_preference(self) -> str:
        return self._engine_combo.currentText()

    def set_search_profile(self, profile: SearchProfile) -> None:
        """Load a saved search profile into the panel."""
        logger.info("[SearchPanel] set_search_profile: paths=%s include=%s exclude=%s pattern=%r",
                    profile.paths, profile.include_rules, profile.exclude_rules, profile.pattern)
        self._pattern_input.setPlainText(profile.pattern)
        if profile.paths:
            logger.info("[SearchPanel] calling _dir_bar.set_rules(%s)", profile.paths)
            # Restore all saved dirs (profile.paths is list[str])
            self._dir_bar.set_rules(profile.paths)
            self._settings.last_directory = profile.paths[0]
            logger.info("[SearchPanel] _dir_bar.rules() after set = %s", self._dir_bar.rules())
        else:
            logger.info("[SearchPanel] profile.paths is empty — dir bar NOT updated")
        self.set_include_rules(profile.include_rules)
        self.set_exclude_rules(profile.exclude_rules)
        self._filter_regex_check.setChecked(profile.use_regex_rules)
        self._regex_btn.setChecked(profile.regex)
        self._case_btn.setChecked(profile.case_sensitive)
        self._word_btn.setChecked(profile.whole_word)
        self.set_engine_preference(profile.engine_preference)
        self._settings.include_rules = profile.include_rules
        self._settings.exclude_rules = profile.exclude_rules
        self._settings.engine_preference = profile.engine_preference
        logger.info("[SearchPanel] set_search_profile complete; dir_bar.rules()=%s", self._dir_bar.rules())

    def set_recent_rules(self, *, include_rules: list[str], exclude_rules: list[str]) -> None:
        """Update persisted recent-rule lists (not shown in chip bars directly)."""
        self._settings.recent_include_rules = list(include_rules)
        self._settings.recent_exclude_rules = list(exclude_rules)

    def current_search_profile(self) -> SearchProfile:
        return SearchProfile.from_query(
            self.get_query(),
            engine_preference=self.engine_preference(),
        )

    def apply_settings(self, settings: AppSettings) -> None:
        """Re-apply settings (e.g. after directory change)."""
        self._settings = settings
        # Only seed the bar when it is empty (don't overwrite user-populated dirs)
        if not self._dir_bar.rules():
            self._dir_bar.set_rules([settings.last_directory])
        if settings.search_history:
            self._pattern_input.setPlainText(settings.search_history[0])
        self._include_bar.set_rules(settings.include_rules)
        self._exclude_bar.set_rules(settings.exclude_rules)
        self.set_engine_preference(settings.engine_preference)

    def get_query(self) -> SearchQuery:
        """Build a SearchQuery from the current panel state."""
        include_rules = self._include_bar.rules()
        exclude_rules = self._exclude_bar.rules()
        file_filter = FileFilter(
            include_rules=include_rules,
            exclude_rules=exclude_rules,
            use_regex=self._filter_regex_check.isChecked(),
        )
        # Collect all dir chips; fall back to home if bar is empty
        dirs = [d for d in self._dir_bar.rules() if d]
        if not dirs:
            dirs = [str(Path.home())]
        self._settings.last_directory = dirs[0]  # keep backward-compat single field
        self._settings.include_rules = include_rules
        self._settings.exclude_rules = exclude_rules
        self._settings.engine_preference = self._engine_combo.currentText()
        return SearchQuery(
            pattern=self._pattern_text(),
            paths=[Path(d) for d in dirs],
            file_filter=file_filter,
            regex=self._regex_btn.isChecked(),
            case_sensitive=self._case_btn.isChecked(),
            whole_word=self._word_btn.isChecked(),
            file_only_mode=self._file_only_mode,
        )

    # ------------------------------------------------------------------
    # Import / Export (delegated from main-window menus)
    # ------------------------------------------------------------------

    def import_rules(self) -> None:
        path_text, _ = QFileDialog.getOpenFileName(
            self, "Import Rules", self._settings.last_directory,
            "Rules Files (*.json *.opq);;All Files (*)",
        )
        if not path_text:
            return
        path = Path(path_text)
        recent_inc: list[str] = []
        recent_exc: list[str] = []
        try:
            if path.suffix.lower() == ".opq":
                profile = SearchProfile.load(path)
                include_rules = profile.include_rules
                exclude_rules = profile.exclude_rules
            else:
                payload = json.loads(path.read_text(encoding="utf-8"))
                include_rules = [str(r).strip() for r in payload.get("include_rules", [])]
                exclude_rules = [str(r).strip() for r in payload.get("exclude_rules", [])]
                recent_inc = [str(r).strip() for r in payload.get("recent_include_rules", [])]
                recent_exc = [str(r).strip() for r in payload.get("recent_exclude_rules", [])]
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            QMessageBox.warning(self, "Import Rules Failed", str(exc))
            return

        # Determine conflict resolution: show dialog if existing rules present
        existing_inc = self._include_bar.rules()
        existing_exc = self._exclude_bar.rules()
        use_merge = False

        if existing_inc or existing_exc:
            msg = QMessageBox(self)
            msg.setWindowTitle("Import Rules — Conflict")
            msg.setText(
                "You already have include/exclude rules.\n"
                "How would you like to handle the imported rules?"
            )
            msg.setIcon(QMessageBox.Icon.Question)
            replace_btn = msg.addButton("Replace All", QMessageBox.ButtonRole.AcceptRole)
            merge_btn = msg.addButton("Merge (keep existing + add new)", QMessageBox.ButtonRole.ActionRole)
            cancel_btn = msg.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
            msg.setDefaultButton(merge_btn)
            msg.exec()

            clicked = msg.clickedButton()
            if clicked is cancel_btn:
                return
            use_merge = clicked is merge_btn

        if use_merge:
            # Union + dedup: keep existing order, append new rules not already present
            inc_set = set(existing_inc)
            exc_set = set(existing_exc)
            merged_inc = existing_inc + [r for r in include_rules if r not in inc_set]
            merged_exc = existing_exc + [r for r in exclude_rules if r not in exc_set]
            include_rules = merged_inc
            exclude_rules = merged_exc
            # Merge recent rules as well
            if recent_inc or recent_exc:
                existing_r_inc = self._settings.recent_include_rules
                existing_r_exc = self._settings.recent_exclude_rules
                merged_r_inc = existing_r_inc + [r for r in recent_inc if r not in set(existing_r_inc)]
                merged_r_exc = existing_r_exc + [r for r in recent_exc if r not in set(existing_r_exc)]
                self.set_recent_rules(include_rules=merged_r_inc, exclude_rules=merged_r_exc)
                recent_inc = []  # handled above; skip set_recent_rules below
                recent_exc = []

        self.set_include_rules(include_rules)
        self.set_exclude_rules(exclude_rules)
        if recent_inc or recent_exc:
            self.set_recent_rules(include_rules=recent_inc, exclude_rules=recent_exc)

    def export_rules(self) -> None:
        path_text, _ = QFileDialog.getSaveFileName(
            self, "Export Rules",
            str(Path(self._settings.last_directory) / "rules.json"),
            "JSON Files (*.json);;All Files (*)",
        )
        if not path_text:
            return
        payload = {
            "include_rules": self._include_bar.rules(),
            "exclude_rules": self._exclude_bar.rules(),
            "recent_include_rules": self._settings.recent_include_rules,
            "recent_exclude_rules": self._settings.recent_exclude_rules,
            "use_regex_rules": self._filter_regex_check.isChecked(),
        }
        try:
            Path(path_text).write_text(
                json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        except OSError as exc:
            QMessageBox.warning(self, "Export Rules Failed", str(exc))
