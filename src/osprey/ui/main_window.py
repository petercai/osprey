"""
osprey.ui.main_window
~~~~~~~~~~~~~~~~~~~~~
Three-panel QMainWindow stacked vertically: [SearchPanel / ResultPanel / PreviewPanel]
Wires up the search worker, replace dialog, and keyboard shortcuts.
"""

from __future__ import annotations

import dataclasses
import logging
from pathlib import Path

from PySide6.QtCore import Qt, Slot
from PySide6.QtGui import QAction, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QFileDialog,
    QMainWindow,
    QMessageBox,
    QSplitter,
    QStatusBar,
    QWidget,
    QVBoxLayout,
)

from osprey.config.settings import AppSettings, SearchProfile
from osprey.engine.registry import EngineRegistry
from osprey.engine.base import FileFilter, SearchResult
from osprey.replace.engine import ReplaceEngine, FileDiff
from osprey.ui.search_panel import SearchPanel
from osprey.ui.result_panel import ResultPanel
from osprey.ui.preview_panel import PreviewPanel
from osprey.ui.workers import SearchWorker

logger = logging.getLogger(__name__)


class MainWindow(QMainWindow):
    """Main application window."""

    def __init__(self) -> None:
        super().__init__()
        self._settings = AppSettings.load()
        self._registry = EngineRegistry()
        self._replace_engine = ReplaceEngine()
        self._worker: SearchWorker | None = None
        # Monotonically-increasing counter: each new search increments this.
        # Workers from previous generations disconnect their signals so stale
        # results never reach the UI, and the parent=self relationship keeps
        # their QThread alive until the OS thread exits (prevents the
        # "QThread: Destroyed while thread is still running" abort).
        self._search_generation: int = 0
        # Most recent SearchResult, used by replace preview
        self._last_result: SearchResult | None = None
        # True when the most recent search was triggered by Find Files
        self._find_files_mode: bool = False
        # Context for in-progress replace preview (inline diff mode)
        self._pending_replace_pattern: str = ""
        self._pending_replace_text: str = ""
        # Map of file-path string → FileDiff for the active diff preview.
        # Kept in sync as per-line / per-file applies are committed.
        self._pending_replace_diffs: dict[str, FileDiff] = {}
        # The file path most recently opened in the preview panel.
        self._preview_current_path: Path | None = None
        # Reference to the main vertical QSplitter — used by collapse/uncollapse.
        self._main_splitter: QSplitter | None = None

        self.setWindowTitle("Osprey")
        self.resize(self._settings.window_width, self._settings.window_height)

        self._build_ui()
        logger.debug("[UI] Osprey main window ready")

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(4, 4, 4, 4)

        # Three-panel vertical splitter for deeper path and result scanning
        splitter = QSplitter(Qt.Orientation.Vertical)

        self._search_panel = SearchPanel(
            available_engines=self._registry.available_names(),
            settings=self._settings,
        )
        self._result_panel = ResultPanel(lazy_load=self._settings.use_virtual_model)
        self._preview_panel = PreviewPanel()

        splitter.addWidget(self._search_panel)
        splitter.addWidget(self._result_panel)
        splitter.addWidget(self._preview_panel)
        # Preview panel is collapsed by default; result panel gets the full space.
        self._preview_panel.setMinimumHeight(0)
        splitter.setCollapsible(2, True)
        splitter.setSizes([320, 880, 0])
        self._main_splitter = splitter

        layout.addWidget(splitter)

        # Status bar
        self._status_bar = QStatusBar()
        self.setStatusBar(self._status_bar)
        self._status_bar.showMessage("Ready")

        # Connect signals
        self._search_panel.search_requested.connect(self._on_search_requested)
        self._search_panel.replace_preview_requested.connect(self._on_replace_preview_requested)
        self._search_panel.replace_all_requested.connect(self._on_replace_all_requested)
        self._result_panel.item_selected.connect(self._on_result_selected)
        self._result_panel.include_rule_requested.connect(self._on_include_rule_requested)
        self._result_panel.exclude_rule_requested.connect(self._on_exclude_rule_requested)
        self._result_panel.apply_change_requested.connect(self._on_apply_replace_change)
        self._result_panel.apply_file_diff_requested.connect(self._on_apply_replace_file_diff)
        self._search_panel.filter_changed.connect(self._on_filter_changed)
        self._result_panel.collapse_preview_requested.connect(self._collapse_preview)
        self._result_panel.uncollapse_preview_requested.connect(self._uncollapse_preview)
        # Line-wrap toggle in the preview title bar → preview panel
        self._result_panel.line_wrap_changed.connect(self._preview_panel.set_line_wrap)
        # Push configured editors to the preview panel so its context menu is ready.
        self._preview_panel.set_editors(self._settings.editors)
        # Apply line-number display preference.
        self._preview_panel.set_show_line_numbers(self._settings.show_line_numbers)

        self._build_menus()
        self._build_shortcuts()

    def _build_shortcuts(self) -> None:
        """Register application-level keyboard shortcuts."""
        # Ctrl+F — focus content filter in result panel
        filter_sc = QShortcut(QKeySequence("Ctrl+F"), self)
        filter_sc.activated.connect(self._result_panel.focus_filter)
        # Ctrl+Enter — trigger search (from anywhere)
        search_sc = QShortcut(QKeySequence("Ctrl+Return"), self)
        search_sc.activated.connect(self._on_search_requested)

    def _build_menus(self) -> None:
        file_menu = self.menuBar().addMenu("&File")

        save_profile_action = QAction("Save Search Options…", self)
        save_profile_action.triggered.connect(self._save_search_options)
        file_menu.addAction(save_profile_action)

        load_profile_action = QAction("Load Search Options…", self)
        load_profile_action.triggered.connect(self._load_search_options)
        file_menu.addAction(load_profile_action)

        file_menu.addSeparator()

        save_results_action = QAction("Save Results…", self)
        save_results_action.setShortcut("Ctrl+S")
        save_results_action.setToolTip("Save current search results to a .opr file")
        save_results_action.triggered.connect(self._save_results)
        file_menu.addAction(save_results_action)

        load_results_action = QAction("Load Results…", self)
        load_results_action.setShortcut("Ctrl+O")
        load_results_action.setToolTip("Load previously saved search results from a .opr file")
        load_results_action.triggered.connect(self._load_results)
        file_menu.addAction(load_results_action)

        file_menu.addSeparator()

        export_rules_action = QAction("Export Rules…", self)
        export_rules_action.setToolTip("Export current include/exclude rules to a JSON file")
        export_rules_action.triggered.connect(self._export_rules)
        file_menu.addAction(export_rules_action)

        import_rules_action = QAction("Import Rules…", self)
        import_rules_action.setToolTip("Load include/exclude rules from a JSON or .opq file")
        import_rules_action.triggered.connect(self._import_rules)
        file_menu.addAction(import_rules_action)

        file_menu.addSeparator()

        settings_action = QAction("Settings…", self)
        settings_action.setShortcut("Ctrl+,")
        settings_action.triggered.connect(self._show_settings)
        file_menu.addAction(settings_action)

        # ── Recent menu ─────────────────────────────────────────────────
        # Two sub-menus: "Recent Queries (.opq)" and "Recent Results (.opr)".
        # Rebuilt whenever an item is opened/saved so the list stays current.
        self._recent_menu = self.menuBar().addMenu("&Recent")
        self._recent_queries_menu = self._recent_menu.addMenu("Recent Queries (.opq)")
        self._recent_results_menu = self._recent_menu.addMenu("Recent Results (.opr)")
        self._rebuild_recent_menu()

    # ------------------------------------------------------------------
    # Recent menu helpers
    # ------------------------------------------------------------------

    def _rebuild_recent_menu(self) -> None:
        """Repopulate both Recent sub-menus from AppSettings.recent_opq/opr."""
        self._populate_recent_submenu(
            self._recent_queries_menu,
            self._settings.recent_opq,
            handler=self._open_recent_opq,
            clear_handler=self._clear_recent_opq,
            empty_label="(no recent queries)",
        )
        self._populate_recent_submenu(
            self._recent_results_menu,
            self._settings.recent_opr,
            handler=self._open_recent_opr,
            clear_handler=self._clear_recent_opr,
            empty_label="(no recent results)",
        )

    @staticmethod
    def _populate_recent_submenu(
        menu: "QMenu",
        paths: list[str],
        *,
        handler,
        clear_handler,
        empty_label: str,
    ) -> None:
        """Clear *menu* and re-populate it with MRU *paths* + a Clear action."""
        menu.clear()
        if not paths:
            placeholder = menu.addAction(empty_label)
            placeholder.setEnabled(False)
        else:
            for path_str in paths:
                p = Path(path_str)
                # Show "filename  (…/parent)" — trim long parent paths to keep UI tidy
                parent = str(p.parent)
                if len(parent) > 50:
                    parent = "…" + parent[-47:]
                label = f"{p.name}   ({parent})"
                action = menu.addAction(label)
                action.setData(path_str)
                action.setToolTip(path_str)
                action.triggered.connect(lambda checked=False, s=path_str: handler(s))
        menu.addSeparator()
        clear_action = menu.addAction("Clear")
        clear_action.triggered.connect(clear_handler)

    def _open_recent_opq(self, path_str: str) -> None:
        """Load a .opq profile from *path_str* and immediately trigger a search."""
        p = Path(path_str)
        if not p.exists():
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.warning(self, "File Not Found", f"Cannot find:\n{path_str}")
            # Remove stale entry
            if path_str in self._settings.recent_opq:
                self._settings.recent_opq.remove(path_str)
                self._settings.save()
                self._rebuild_recent_menu()
            return
        try:
            profile = SearchProfile.load(p)
            self._search_panel.set_search_profile(profile)
            if profile.paths:
                self._settings.last_directory = profile.paths[0]
            self._settings.engine_preference = profile.engine_preference
            self._settings.push_recent_opq(path_str)
            self._settings.save()
            self._rebuild_recent_menu()
            self._status_bar.showMessage(f"Loaded query profile: {p.name} — running search…")
            logger.debug("[UI] Recent .opq opened: %s", path_str)
            # Auto-trigger the search so the user sees results immediately.
            self._on_search_requested()
        except (OSError, ValueError, KeyError) as exc:
            self._status_bar.showMessage(f"Failed to load {p.name}: {exc}")
            logger.error("[UI] Failed to open recent .opq %s: %s", path_str, exc)

    def _open_recent_opr(self, path_str: str) -> None:
        """Load a .opr results snapshot from *path_str*."""
        p = Path(path_str)
        if not p.exists():
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.warning(self, "File Not Found", f"Cannot find:\n{path_str}")
            if path_str in self._settings.recent_opr:
                self._settings.recent_opr.remove(path_str)
                self._settings.save()
                self._rebuild_recent_menu()
            return
        self._settings.push_recent_opr(path_str)
        self._settings.save()
        self._rebuild_recent_menu()
        logger.debug("[UI] Recent .opr opened: %s", path_str)
        self._load_results_from_path(path_str)

    def _clear_recent_opq(self) -> None:
        """Clear the recent queries MRU list."""
        self._settings.recent_opq.clear()
        self._settings.save()
        self._rebuild_recent_menu()
        logger.debug("[UI] Recent .opq list cleared")

    def _clear_recent_opr(self) -> None:
        """Clear the recent results MRU list."""
        self._settings.recent_opr.clear()
        self._settings.save()
        self._rebuild_recent_menu()
        logger.debug("[UI] Recent .opr list cleared")

    def _profile_file_name(self) -> str:
        return "osprey_search.opq"

    # ------------------------------------------------------------------
    # Search slots
    # ------------------------------------------------------------------

    @Slot()
    def _on_search_requested(self) -> None:
        """Triggered when the user clicks Search or presses Enter."""
        self._find_files_mode = self._search_panel.is_find_files_mode
        query = self._search_panel.get_query()
        if not query.pattern:
            self._status_bar.showMessage("Enter a search pattern")
            return

        # Respect the engine selected by the user; fall back to best available
        pref = self._search_panel.engine_preference()
        engine = self._registry.get_engine(pref)
        if engine is None:
            try:
                engine = self._registry.best_engine()
            except RuntimeError:
                self._status_bar.showMessage("No search engine available (install rg, ag, or grep)")
                logger.error("[UI] No search engine found")
                return
            logger.warning("[UI] Preferred engine '%s' not available, falling back to '%s'", pref, engine.name())

        self._result_panel.clear()
        self._last_result = None
        self._pending_replace_diffs = {}
        self._status_bar.showMessage(f"Searching in {query.paths[0]}…")

        # Increment generation so any still-running worker's result is ignored.
        self._search_generation += 1
        gen = self._search_generation

        # Disconnect previous worker's signals to discard its result if still running.
        # parent=self keeps the QThread alive (C++ parent-child) until finished;
        # finished→deleteLater then removes it safely from the parent's children.
        if self._worker is not None:
            try:
                self._worker.result_ready.disconnect(self._on_search_done)
                self._worker.error.disconnect(self._on_search_error)
            except RuntimeError:
                pass  # already disconnected (worker finished before reassignment)

        worker = SearchWorker(engine=engine, query=query, parent=self)
        worker.result_ready.connect(
            lambda result, _g=gen: self._on_search_done(result) if _g == self._search_generation else None
        )
        worker.error.connect(
            lambda msg, _g=gen: self._on_search_error(msg) if _g == self._search_generation else None
        )
        # Clean up the C++ QThread object once the OS thread exits.
        worker.finished.connect(worker.deleteLater)
        self._worker = worker
        logger.debug("[UI] Search gen=%d started (engine=%s)", gen, engine.name())
        worker.start()

    @Slot(SearchResult)
    def _on_search_done(self, result: SearchResult) -> None:
        self._last_result = result
        self._result_panel.populate(result, file_only=self._find_files_mode)
        self._preview_panel.set_match_pattern(
            result.query.pattern,
            regex=result.query.regex,
            case_sensitive=result.query.case_sensitive,
            whole_word=result.query.whole_word,
        )
        self._status_bar.showMessage(
            f"{result.total_file_count} files / {result.total_match_count} matches "
            f"— {result.elapsed_ms:.0f} ms  [{result.engine_used}]"
        )
        self._settings.push_history(result.query.pattern)
        logger.debug(
            "[UI] Search done: %d files, %d matches, %.0f ms",
            result.total_file_count, result.total_match_count, result.elapsed_ms,
        )

    @Slot(str)
    def _on_search_error(self, message: str) -> None:
        self._status_bar.showMessage(f"Error: {message}")
        logger.error("[UI] Search error: %s", message)

    # ------------------------------------------------------------------
    # Preview panel collapse / uncollapse
    # ------------------------------------------------------------------

    @Slot()
    def _collapse_preview(self) -> None:
        """Collapse the preview panel to zero height via the splitter."""
        if self._main_splitter is None:
            return
        sizes = self._main_splitter.sizes()
        if len(sizes) < 3 or sizes[2] == 0:
            return  # already collapsed
        total_rp = sizes[1] + sizes[2]
        self._main_splitter.setSizes([sizes[0], total_rp, 0])
        logger.debug("[UI] Preview panel collapsed")

    @Slot()
    def _uncollapse_preview(self) -> None:
        """Expand the preview panel to half the current result panel height."""
        if self._main_splitter is None:
            return
        sizes = self._main_splitter.sizes()
        if len(sizes) < 3 or sizes[2] > 0:
            return  # already visible
        # Give preview half of what the result panel currently occupies.
        half = max(sizes[1] // 2, 150)
        self._main_splitter.setSizes([sizes[0], sizes[1] - half, half])
        logger.debug("[UI] Preview panel uncollapsed (height=%d)", half)

    @Slot(str, int)
    def _on_result_selected(self, file_path: str, line_number: int) -> None:
        self._preview_current_path = Path(file_path)
        # Auto-uncollapse the preview panel whenever the user selects a result.
        self._uncollapse_preview()
        # If a diff preview is active and this file has pending changes, show
        # the diff overlay in the preview panel; otherwise show the plain file.
        if self._pending_replace_diffs:
            diff = self._pending_replace_diffs.get(file_path)
            if diff is not None:
                # scroll_to=line_number positions the preview on the exact
                # diff_change line the user clicked (0 → first pending change).
                self._preview_panel.show_diff_for_file(diff, scroll_to=line_number)
                return
        self._preview_panel.show_file(Path(file_path), line_number)

    @Slot(str)
    def _on_include_rule_requested(self, rule: str) -> None:
        self._search_panel.add_include_rule(rule)
        self._status_bar.showMessage(f"Included rule added: {rule}")

    @Slot(str)
    def _on_exclude_rule_requested(self, rule: str) -> None:
        self._search_panel.add_exclude_rule(rule)
        self._status_bar.showMessage(f"Excluded rule added: {rule}")

    @Slot()
    def _on_filter_changed(self) -> None:
        """Re-apply current include/exclude chip rules to the raw result in memory.

        Called whenever the chip bars change; avoids a full re-search for filter
        adjustments that can be satisfied in-memory.
        """
        if self._last_result is None:
            return
        profile = self._search_panel.current_search_profile()
        file_filter = FileFilter(
            include_rules=profile.include_rules,
            exclude_rules=profile.exclude_rules,
            use_regex=profile.use_regex_rules,
        )
        # Embed the current filter in the query so the result panel's _last_result
        # carries the correct include/exclude state for future .opr saves.
        updated_query = dataclasses.replace(self._last_result.query, file_filter=file_filter)
        updated_result = dataclasses.replace(self._last_result, query=updated_query)
        filtered = file_filter.filter_result(updated_result)
        self._result_panel.populate(filtered, file_only=self._find_files_mode)
        self._status_bar.showMessage(
            f"{filtered.total_file_count} files / {filtered.total_match_count} matches "
            f"(filtered)  [{updated_result.engine_used}]"
        )
        logger.debug(
            "[UI] Filter changed: include=%s exclude=%s → %d files shown",
            profile.include_rules, profile.exclude_rules, filtered.total_file_count,
        )

    # ------------------------------------------------------------------
    # Replace preview / apply slots  (inline diff in result panel)
    # ------------------------------------------------------------------

    @Slot(str, str)
    def _on_replace_preview_requested(self, pattern: str, replacement: str) -> None:
        """Compute diffs and switch result panel to inline diff preview mode."""
        if self._last_result is None or not self._last_result.files:
            self._status_bar.showMessage("No search results to preview replacement")
            return

        matched_files = [fm.file_path for fm in self._last_result.files]
        query = self._last_result.query
        logger.info(
            "[UI] Replace preview: pattern=%r replacement=%r files=%d",
            pattern, replacement, len(matched_files),
        )

        diffs = self._replace_engine.preview(
            pattern=pattern,
            replacement=replacement,
            files=matched_files,
            regex=query.regex,
            case_sensitive=query.case_sensitive,
        )
        if not diffs:
            self._status_bar.showMessage(
                "No replacements found — pattern not matched in current results"
            )
            return

        # Cache for individual apply handlers
        self._pending_replace_pattern = pattern
        self._pending_replace_text = replacement

        self._result_panel.show_diff_preview(diffs)
        # Build the file-path → FileDiff lookup used by preview panel updates.
        self._pending_replace_diffs = {str(d.file_path): d for d in diffs}
        # Update preview panel if the currently shown file has pending changes.
        if self._preview_current_path is not None:
            diff = self._pending_replace_diffs.get(str(self._preview_current_path))
            if diff is not None:
                self._preview_panel.show_diff_for_file(diff)
                logger.debug(
                    "[UI] Preview panel diff overlay: %s", self._preview_current_path.name
                )
        total_changes = sum(d.change_count for d in diffs)
        self._status_bar.showMessage(
            f"Replace preview: {total_changes} change(s) in {len(diffs)} file(s) "
            "— click \u21a9 to apply a single change, or 'Replace File \u21a9' for all in file"
        )

    @Slot(str, str)
    def _on_replace_all_requested(self, pattern: str, replacement: str) -> None:
        """Apply all replacements immediately after user confirmation."""
        if self._last_result is None or not self._last_result.files:
            self._status_bar.showMessage("No search results to replace")
            return

        matched_files = [fm.file_path for fm in self._last_result.files]
        query = self._last_result.query

        diffs = self._replace_engine.preview(
            pattern=pattern,
            replacement=replacement,
            files=matched_files,
            regex=query.regex,
            case_sensitive=query.case_sensitive,
        )
        if not diffs:
            self._status_bar.showMessage("No replacements found")
            return

        total = sum(d.change_count for d in diffs)
        backup = self._search_panel.backup_enabled
        backup_note = "Backups will be saved as *.osprey.bak." if backup else "No backup will be created."
        reply = QMessageBox.question(
            self,
            "Replace All",
            f"Apply {total} change(s) across {len(diffs)} file(s)?\n{backup_note}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            self._status_bar.showMessage("Replace All cancelled")
            return

        session = self._replace_engine.apply(diffs, backup=backup)
        self._replace_engine.write_audit_log(
            pattern=pattern, replacement=replacement, session=session, diffs=diffs
        )
        # Clear the result panel — file content has changed on disk
        self._result_panel.clear()
        self._last_result = None
        self._pending_replace_diffs = {}
        self._status_bar.showMessage(
            f"Replaced {total} occurrence(s) in {len(diffs)} file(s)"
            f" — backups saved as *.osprey.bak; session {session.session_id[:8]}"
        )
        logger.info(
            "[UI] Replace All: session=%s files=%d changes=%d",
            session.session_id, len(diffs), total,
        )

    @Slot(object, object)
    def _on_apply_replace_change(self, file_diff: object, change: object) -> None:
        """Apply a single line change from the diff preview."""
        success = self._replace_engine.apply_partial(
            file_diff, [change], backup=self._search_panel.backup_enabled  # type: ignore[arg-type]
        )
        if success:
            # Keep the row in the result panel — only mark as applied (hide ↩ button,
            # re-render in green "applied" style).
            self._result_panel.mark_change_applied(file_diff.file_path, change.line_number)  # type: ignore[union-attr]
            self._status_bar.showMessage(
                f"Applied change at L{change.line_number} in {file_diff.file_path.name}"  # type: ignore[union-attr]
            )
            logger.info(
                "[UI] Single change applied: %s L%d",
                file_diff.file_path, change.line_number,  # type: ignore[union-attr]
            )
            self._sync_preview_after_change(file_diff, change)  # type: ignore[arg-type]
        else:
            self._status_bar.showMessage(
                f"Could not apply change at L{change.line_number} — line may have been modified"  # type: ignore[union-attr]
            )

    def _sync_preview_after_change(self, file_diff: object, change: object) -> None:
        """Update *_pending_replace_diffs* and refresh the preview panel after
        a single line change has been written to disk.

        Re-reads the file from disk (freshest content) and renders the
        preview with:
        - Remaining *pending* changes as red/green diff overlay.
        - The just-applied line highlighted in yellow (match style).
        """
        path_key = str(file_diff.file_path)  # type: ignore[union-attr]
        applied_line: int = change.line_number  # type: ignore[union-attr]

        if path_key not in self._pending_replace_diffs:
            return

        current_diff: FileDiff = self._pending_replace_diffs[path_key]
        remaining = [c for c in current_diff.changes if c.line_number != applied_line]

        # Re-read the file from disk; the applied change is already on disk.
        try:
            new_original = file_diff.file_path.read_text(  # type: ignore[union-attr]
                encoding="utf-8", errors="replace"
            ).splitlines(keepends=True)
        except OSError:
            new_original = current_diff.original_lines

        updated_diff = FileDiff(
            file_path=current_diff.file_path,
            original_lines=new_original,
            patched_lines=current_diff.patched_lines,
            changes=remaining,
        )

        if remaining:
            self._pending_replace_diffs[path_key] = updated_diff
        else:
            # All changes applied — no more pending diffs for this file.
            del self._pending_replace_diffs[path_key]

        # Use the preview panel's own tracking of which file is shown in diff
        # mode — more reliable than _preview_current_path because the ↩ button
        # click does NOT trigger _on_item_clicked / _on_result_selected.
        if self._preview_panel.diff_file_path == file_diff.file_path:  # type: ignore[union-attr]
            self._preview_panel.show_diff_for_file(
                updated_diff, highlighted=frozenset([applied_line])
            )
            logger.debug(
                "[UI] Preview sync: %s — %d pending, L%d highlighted",
                current_diff.file_path.name, len(remaining), applied_line,
            )

    @Slot(object)
    def _on_apply_replace_file_diff(self, file_diff: object) -> None:
        """Apply all changes in one file from the diff preview."""
        session = self._replace_engine.apply(
            [file_diff], backup=self._search_panel.backup_enabled  # type: ignore[list-item]
        )
        if session.is_committed:
            self._replace_engine.write_audit_log(
                pattern=self._pending_replace_pattern,
                replacement=self._pending_replace_text,
                session=session,
                diffs=[file_diff],  # type: ignore[list-item]
            )
            # Keep nodes in the result panel — mark all as applied (hide ↩ buttons).
            self._result_panel.mark_file_applied(file_diff.file_path)  # type: ignore[union-attr]
            self._status_bar.showMessage(
                f"Applied {file_diff.change_count} change(s) in {file_diff.file_path.name}"  # type: ignore[union-attr]
            )
            logger.info(
                "[UI] File diff applied: %s (%d changes)",
                file_diff.file_path, file_diff.change_count,  # type: ignore[union-attr]
            )
            # Remove from pending diffs; update preview panel with applied highlights.
            path_key = str(file_diff.file_path)  # type: ignore[union-attr]
            applied_lines = frozenset(c.line_number for c in file_diff.changes)  # type: ignore[union-attr]
            self._pending_replace_diffs.pop(path_key, None)
            if self._preview_panel.diff_file_path == file_diff.file_path:  # type: ignore[union-attr]
                try:
                    new_lines = file_diff.file_path.read_text(  # type: ignore[union-attr]
                        encoding="utf-8", errors="replace"
                    ).splitlines(keepends=True)
                except OSError:
                    new_lines = file_diff.patched_lines  # type: ignore[union-attr]
                display_diff = FileDiff(
                    file_path=file_diff.file_path,  # type: ignore[union-attr]
                    original_lines=new_lines,
                    patched_lines=new_lines,
                    changes=[],
                )
                self._preview_panel.show_diff_for_file(
                    display_diff, highlighted=applied_lines
                )
                logger.debug(
                    "[UI] Preview panel: %s — %d changes highlighted",
                    file_diff.file_path.name, len(applied_lines),  # type: ignore[union-attr]
                )
        else:
            self._status_bar.showMessage("Replace failed — check logs for details")

    # ------------------------------------------------------------------
    # Search profile commands
    # ------------------------------------------------------------------

    @Slot()
    def _save_search_options(self) -> None:
        profile = self._search_panel.current_search_profile()
        start_dir = Path(profile.paths[0]) if profile.paths else Path(self._settings.last_directory)
        default_path = start_dir / self._profile_file_name()
        target, _ = QFileDialog.getSaveFileName(
            self,
            "Save Search Options",
            str(default_path),
            "Osprey Query (*.opq)",
        )
        if not target:
            return

        target_path = Path(target)
        if target_path.suffix.lower() != ".opq":
            target_path = target_path.with_suffix(".opq")

        try:
            profile.save(target_path)
            self._settings.push_recent_opq(str(target_path))
            self._settings.save()
            self._rebuild_recent_menu()
            self._status_bar.showMessage(f"Search options saved to {target_path}")
            logger.debug("[UI] Search profile saved to %s", target_path)
        except OSError as exc:
            self._status_bar.showMessage(f"Failed to save search options: {exc}")
            logger.error("[UI] Failed to save search profile: %s", exc)

    @Slot()
    def _load_search_options(self) -> None:
        start_dir = Path(self._settings.last_directory)
        selected, _ = QFileDialog.getOpenFileName(
            self,
            "Load Search Options",
            str(start_dir),
            "Osprey Query (*.opq)",
        )
        if not selected:
            return

        try:
            profile = SearchProfile.load(Path(selected))
            self._search_panel.set_search_profile(profile)
            if profile.paths:
                self._settings.last_directory = profile.paths[0]
            self._settings.engine_preference = profile.engine_preference
            self._settings.push_recent_opq(selected)
            self._settings.save()
            self._rebuild_recent_menu()
            self._status_bar.showMessage(f"Search options loaded from {selected}")
            logger.debug("[UI] Search profile loaded from %s", selected)
        except (OSError, ValueError, KeyError) as exc:
            self._status_bar.showMessage(f"Failed to load search options: {exc}")
            logger.error("[UI] Failed to load search profile: %s", exc)

    def _load_search_options_from_path(self, path: str) -> None:
        """Load a .opq profile from *path*; used by CLI --profile arg."""
        try:
            profile = SearchProfile.load(Path(path))
            self._search_panel.set_search_profile(profile)
            if profile.paths:
                self._settings.last_directory = profile.paths[0]
            self._settings.engine_preference = profile.engine_preference
            logger.debug("[UI] Profile loaded from CLI arg: %s", path)
        except (OSError, ValueError, KeyError) as exc:
            logger.warning("[UI] CLI --profile load failed: %s", exc)

    @Slot()
    def _export_rules(self) -> None:
        """Export current include/exclude rules (plus recent rule lists) to a JSON file."""
        self._search_panel.export_rules()

    @Slot()
    def _import_rules(self) -> None:
        """Load include/exclude rules from a JSON or .opq file."""
        self._search_panel.import_rules()

    # ------------------------------------------------------------------
    # Save / Load Results (.opr)
    # ------------------------------------------------------------------

    @Slot()
    def _save_results(self) -> None:
        """Save the current search results to a .opr snapshot file."""
        result = self._result_panel.current_result()
        if result is None:
            self._status_bar.showMessage("No search results to save")
            return

        start_dir = result.query.paths[0] if result.query.paths else Path(self._settings.last_directory)
        default_path = start_dir / "osprey_search_result.opr"
        target, _ = QFileDialog.getSaveFileName(
            self,
            "Save Search Results",
            str(default_path),
            "Osprey Results (*.opr)",
        )
        if not target:
            return

        target_path = Path(target)
        if target_path.suffix.lower() != ".opr":
            target_path = target_path.with_suffix(".opr")

        try:
            from osprey.results.io import save as result_save
            # Ensure the saved .opr reflects the current chip-bar filter state even
            # when the user changed chips after the search without re-running it.
            profile = self._search_panel.current_search_profile()
            current_filter = FileFilter(
                include_rules=profile.include_rules,
                exclude_rules=profile.exclude_rules,
                use_regex=profile.use_regex_rules,
            )
            result_to_save = dataclasses.replace(
                result,
                query=dataclasses.replace(result.query, file_filter=current_filter),
            )
            result_save(result_to_save, target_path)
            self._settings.push_recent_opr(str(target_path))
            self._settings.save()
            self._rebuild_recent_menu()
            self._status_bar.showMessage(
                f"Results saved: {result_to_save.total_file_count} files / "
                f"{result_to_save.total_match_count} matches → {target_path.name}"
            )
            logger.debug("[UI] Results saved to %s", target_path)
        except OSError as exc:
            QMessageBox.warning(self, "Save Results Failed", str(exc))
            logger.error("[UI] Failed to save results: %s", exc)

    @Slot()
    def _load_results(self) -> None:
        """Load a previously saved .opr results snapshot."""
        start_dir = Path(self._settings.last_directory)
        selected, _ = QFileDialog.getOpenFileName(
            self,
            "Load Search Results",
            str(start_dir),
            "Osprey Results (*.opr);;All Files (*)",
        )
        if not selected:
            return
        self._settings.push_recent_opr(selected)
        self._settings.save()
        self._rebuild_recent_menu()
        self._load_results_from_path(selected)

    def _load_results_from_path(self, path: str) -> None:
        """Load results from *path*; used by both menu action and CLI arg."""
        logger.debug("[UI] _load_results_from_path: opening %s", path)
        try:
            from osprey.results.io import load as result_load, to_search_result
            snapshot = result_load(Path(path))
            logger.debug("[UI] .opr parsed: paths=%s include_rules=%s pattern=%r",
                        snapshot.paths, snapshot.include_rules, snapshot.pattern)
            # Populate the result panel first.  _last_result is None at this point
            # so filter_changed from set_search_profile is a no-op in _on_filter_changed.
            self._result_panel.load_from_snapshot(snapshot)
            # Restore all search panel fields via the same path as .opq loading so
            # the dir-bar chip update follows the identical code path that is proven
            # to work.  set_search_profile handles dirs, pattern, include/exclude, and
            # all toggle buttons in one consistent call.
            profile = SearchProfile(
                pattern=snapshot.pattern,
                paths=snapshot.paths,
                include_rules=snapshot.include_rules,
                exclude_rules=snapshot.exclude_rules,
                use_regex_rules=snapshot.use_regex_rules,
                regex=snapshot.regex,
                case_sensitive=snapshot.case_sensitive,
                whole_word=snapshot.whole_word,
                engine_preference=self._search_panel.engine_preference(),
            )
            logger.debug("[UI] calling set_search_profile with paths=%s", profile.paths)
            self._search_panel.set_search_profile(profile)
            logger.debug("[UI] set_search_profile done; dir_bar now has: %s",
                        self._search_panel.directories())
            if snapshot.paths:
                logger.debug("[UI] .opr load: search dir(s) restored: %s", snapshot.paths)
            else:
                logger.debug("[UI] .opr load: snapshot has no paths — dir bar unchanged")
            # Set _last_result AFTER panel is fully configured so future filter_changed
            # events correctly re-filter these results.
            self._last_result = to_search_result(snapshot)
            self._preview_panel.set_match_pattern(
                snapshot.pattern,
                regex=snapshot.regex,
                case_sensitive=snapshot.case_sensitive,
                whole_word=snapshot.whole_word,
            )
            self._status_bar.showMessage(
                f"Loaded: {snapshot.total_files} files / {snapshot.total_matches} matches "
                f"— saved {snapshot.saved_at[:10]}"
            )
            logger.debug("[UI] Results loaded from %s", path)
        except (OSError, ValueError, KeyError) as exc:
            QMessageBox.warning(self, "Load Results Failed", str(exc))
            logger.error("[UI] Failed to load results: %s", exc)

    # ------------------------------------------------------------------
    # Settings dialog
    # ------------------------------------------------------------------

    @Slot()
    def _show_settings(self) -> None:
        """Open the application settings dialog."""
        from osprey.ui.settings_dialog import SettingsDialog
        dlg = SettingsDialog(self._settings, parent=self)
        if dlg.exec():
            # Persist the updated settings immediately
            self._settings.save()
            # Refresh preview panel editor list
            self._preview_panel.set_editors(self._settings.editors)
            # Refresh preview panel line-number display preference.
            self._preview_panel.set_show_line_numbers(self._settings.show_line_numbers)
            logger.info(
                "[UI] Settings saved; editors=%d line_numbers=%s",
                len(self._settings.editors), self._settings.show_line_numbers,
            )
            # Rebuild result panel with the new model backend if needed
            new_lazy = self._settings.use_virtual_model
            if new_lazy != self._result_panel._lazy_load:
                self._result_panel._lazy_load = new_lazy
                self._result_panel._model = type(self._result_panel._model)(
                    self._result_panel, lazy_load=new_lazy
                )
                self._result_panel._tree.setModel(self._result_panel._model)
                self._result_panel.clear()
                self._status_bar.showMessage(
                    "Tree model switched to "
                    + ("virtual/lazy" if new_lazy else "plain")
                    + " — results cleared"
                )
                logger.debug("[UI] Tree model backend changed: lazy_load=%s", new_lazy)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def changeDirectory(self, new_dir: str) -> None:
        """Called when the user changes the working directory."""
        new_path = Path(new_dir)
        self._settings = AppSettings.load_for_directory(new_path)
        self._search_panel.apply_settings(self._settings)
        # Reflect project config status in status bar
        project_cfg = new_path / ".osprey" / "settings.json"
        if project_cfg.exists():
            self._status_bar.showMessage(f"Project config loaded (.osprey)  —  {new_dir}")
        logger.debug("[UI] Directory changed to %s", new_dir)

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self._settings.window_width = self.width()
        self._settings.window_height = self.height()
        self._settings.save()
        super().closeEvent(event)
