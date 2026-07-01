"""
osprey.ui.settings_dialog
~~~~~~~~~~~~~~~~~~~~~~~~~
Application preferences dialog — Fork.app-style multi-tab layout.

Tabs
----
General
    Result tree model backend (plain vs virtual/lazy).

Editor
    List of external editors launched from the Preview panel context menu.
    Each entry has a *name* (shown in the menu) and a *command* template
    with ``{file}`` and ``{line}`` placeholders.
"""
from __future__ import annotations

import logging

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QRadioButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from osprey.config.settings import AppSettings, EditorConfig

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal sub-dialog for add / edit a single editor entry
# ---------------------------------------------------------------------------

class _EditorEditDialog(QDialog):
    """Compact form dialog for configuring one external editor entry."""

    def __init__(
        self,
        name: str = "",
        command: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Configure Editor")
        self.setMinimumWidth(440)
        self._build_ui(name, command)

    def _build_ui(self, name: str, command: str) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 8)
        layout.setSpacing(8)

        form = QFormLayout()
        form.setSpacing(6)

        self._name_edit = QLineEdit(name)
        self._name_edit.setPlaceholderText("e.g. VSCode")
        form.addRow("Name:", self._name_edit)

        self._cmd_edit = QLineEdit(command)
        self._cmd_edit.setPlaceholderText("e.g. code --goto {file}:{line}")
        form.addRow("Command:", self._cmd_edit)
        layout.addLayout(form)

        hint = QLabel(
            "Placeholders:  <b>{file}</b> = absolute file path"
            "  \u00b7  <b>{line}</b> = 1-based line number\n"
            "Use <b>{file}:{line}</b> together; the <code>:{line}</code> part is "
            "omitted automatically when no line number is available."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("QLabel{color:palette(mid)}")
        layout.addWidget(hint)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._validate_and_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _validate_and_accept(self) -> None:
        if not self._name_edit.text().strip():
            self._name_edit.setFocus()
            return
        if not self._cmd_edit.text().strip():
            self._cmd_edit.setFocus()
            return
        self.accept()

    @property
    def editor_name(self) -> str:
        return self._name_edit.text().strip()

    @property
    def editor_command(self) -> str:
        return self._cmd_edit.text().strip()


# ---------------------------------------------------------------------------
# Main settings dialog
# ---------------------------------------------------------------------------

class SettingsDialog(QDialog):
    """Modal preferences dialog that reads/writes AppSettings fields.

    Call ``exec()``; on OK the *settings* object is updated in-place.
    """

    def __init__(self, settings: AppSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._settings = settings
        self.setWindowTitle("Osprey \u2014 Preferences")
        self.setMinimumWidth(520)
        self._build_ui()
        self._load_values()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 8)
        root.setSpacing(10)

        tabs = QTabWidget()
        tabs.addTab(self._build_general_tab(), "General")
        tabs.addTab(self._build_editor_tab(), "Editor")
        root.addWidget(tabs)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    # ── General tab ───────────────────────────────────────────────────

    def _build_general_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)
        layout.addWidget(self._build_tree_model_group())
        layout.addWidget(self._build_preview_group())
        layout.addStretch()
        return tab

    def _build_preview_group(self) -> QGroupBox:
        group = QGroupBox("Preview Panel")
        form = QVBoxLayout(group)
        form.setSpacing(6)

        self._chk_line_numbers = QCheckBox("Show line numbers")
        self._chk_line_numbers.setToolTip(
            "Display 1-based line numbers at the left of each line in the "
            "Preview panel (normal view only — not applied to diff overlays)."
        )
        form.addWidget(self._chk_line_numbers)
        return group

    def _build_tree_model_group(self) -> QGroupBox:
        group = QGroupBox("Result Tree Model Backend")
        form = QVBoxLayout(group)
        form.setSpacing(6)

        desc = QLabel(
            "Choose how search results are loaded into the tree view.\n"
            "Plain is simpler; Virtual handles large result sets more efficiently."
        )
        desc.setWordWrap(True)
        desc.setStyleSheet("QLabel{color:palette(mid)}")
        form.addWidget(desc)

        self._radio_plain = QRadioButton(
            "Plain \u2014 load all results immediately (recommended for < 5,000 files)"
        )
        self._radio_virtual = QRadioButton(
            "Virtual / lazy \u2014 on-demand loading (recommended for large result sets)"
        )
        form.addWidget(self._radio_plain)
        form.addWidget(self._radio_virtual)
        return group

    # ── Editor tab ────────────────────────────────────────────────────

    def _build_editor_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)
        layout.addWidget(self._build_editor_group())
        layout.addStretch()
        return tab

    def _build_editor_group(self) -> QGroupBox:
        group = QGroupBox("External Editors")
        g_layout = QVBoxLayout(group)
        g_layout.setSpacing(6)

        desc = QLabel(
            "Editors listed here appear in the Preview panel right-click menu as "
            "\u201cOpen with \u2026\u201d entries."
        )
        desc.setWordWrap(True)
        desc.setStyleSheet("QLabel{color:palette(mid)}")
        g_layout.addWidget(desc)

        # Editor list widget
        self._editor_list = QListWidget()
        self._editor_list.setAlternatingRowColors(True)
        self._editor_list.setMinimumHeight(120)
        self._editor_list.itemDoubleClicked.connect(self._on_edit_editor)
        g_layout.addWidget(self._editor_list)

        # Add / Edit / Remove buttons
        btn_layout = QHBoxLayout()
        btn_add = QPushButton("Add\u2026")
        btn_add.setToolTip("Add a new external editor")
        btn_add.clicked.connect(self._on_add_editor)

        self._btn_edit = QPushButton("Edit\u2026")
        self._btn_edit.setToolTip("Edit the selected editor (or double-click)")
        self._btn_edit.clicked.connect(self._on_edit_editor)

        self._btn_remove = QPushButton("Remove")
        self._btn_remove.setToolTip("Remove the selected editor")
        self._btn_remove.clicked.connect(self._on_remove_editor)

        btn_layout.addWidget(btn_add)
        btn_layout.addWidget(self._btn_edit)
        btn_layout.addWidget(self._btn_remove)
        btn_layout.addStretch()
        g_layout.addLayout(btn_layout)

        hint = QLabel(
            "Template variables: <b>{file}</b> = file path  \u00b7  "
            "<b>{line}</b> = line number  \u00b7  "
            "Use <b>{file}:{line}</b> for line-aware editors"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("QLabel{color:palette(mid); font-size:10pt}")
        g_layout.addWidget(hint)
        return group

    # ------------------------------------------------------------------
    # Value load / save
    # ------------------------------------------------------------------

    def _load_values(self) -> None:
        # General tab
        if self._settings.use_virtual_model:
            self._radio_virtual.setChecked(True)
        else:
            self._radio_plain.setChecked(True)
        self._chk_line_numbers.setChecked(self._settings.show_line_numbers)

        # Editor tab — populate list from current settings
        for ed in self._settings.editors:
            self._append_editor_item(ed)

    def _append_editor_item(self, ed: EditorConfig) -> None:
        """Add *ed* as a new list item storing the config as UserRole data."""
        item = QListWidgetItem(f"{ed.name}  \u2014  {ed.command}")
        item.setData(Qt.ItemDataRole.UserRole, ed)
        self._editor_list.addItem(item)

    # ── Editor CRUD handlers ──────────────────────────────────────────

    def _on_add_editor(self) -> None:
        dlg = _EditorEditDialog(parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            ed = EditorConfig(name=dlg.editor_name, command=dlg.editor_command)
            self._append_editor_item(ed)
            self._editor_list.setCurrentRow(self._editor_list.count() - 1)
            logger.debug("[Settings] Editor added: %r", ed.name)

    def _on_edit_editor(self) -> None:
        item = self._editor_list.currentItem()
        if item is None:
            return
        ed: EditorConfig = item.data(Qt.ItemDataRole.UserRole)
        dlg = _EditorEditDialog(name=ed.name, command=ed.command, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            new_ed = EditorConfig(name=dlg.editor_name, command=dlg.editor_command)
            item.setText(f"{new_ed.name}  \u2014  {new_ed.command}")
            item.setData(Qt.ItemDataRole.UserRole, new_ed)
            logger.debug("[Settings] Editor updated: %r", new_ed.name)

    def _on_remove_editor(self) -> None:
        row = self._editor_list.currentRow()
        if row >= 0:
            removed = self._editor_list.item(row).data(Qt.ItemDataRole.UserRole)
            self._editor_list.takeItem(row)
            logger.debug("[Settings] Editor removed: %r", getattr(removed, "name", "?"))

    # ── Accept / reject ───────────────────────────────────────────────

    def _on_accept(self) -> None:
        # --- General settings ---
        old_virtual = self._settings.use_virtual_model
        self._settings.use_virtual_model = self._radio_virtual.isChecked()
        if old_virtual != self._settings.use_virtual_model:
            logger.info(
                "[Settings] use_virtual_model changed: %s -> %s",
                old_virtual, self._settings.use_virtual_model,
            )

        old_line_numbers = self._settings.show_line_numbers
        self._settings.show_line_numbers = self._chk_line_numbers.isChecked()
        if old_line_numbers != self._settings.show_line_numbers:
            logger.info(
                "[Settings] show_line_numbers changed: %s -> %s",
                old_line_numbers, self._settings.show_line_numbers,
            )

        # --- Editor list ---
        editors: list[EditorConfig] = [
            self._editor_list.item(i).data(Qt.ItemDataRole.UserRole)
            for i in range(self._editor_list.count())
        ]
        self._settings.editors = editors
        logger.info("[Settings] Editors saved: %d configured", len(editors))

        self.accept()

