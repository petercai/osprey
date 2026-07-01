"""
osprey.config.settings
~~~~~~~~~~~~~~~~~~~~~~
Application settings — two-level config system.

Level 1 (global):  ~/.config/osprey/settings.json  (platformdirs)
Level 2 (project): <workdir>/.osprey/settings.json

Project settings perform a field-level overlay on top of global settings.
Fields absent from the project file inherit the global value.

Merge priority: project > global > built-in defaults.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import re
import shlex
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

# Suppress the Win32 console flash when launching child processes from a
# GUI-mode executable.  0x08000000 == CREATE_NO_WINDOW; POSIX ignores it.
_CREATE_NO_WINDOW: int = 0x08000000 if sys.platform == "win32" else 0

_APP_NAME = "osprey"
_SETTINGS_FILENAME = "settings.json"
_PROJECT_DIR = ".osprey"
_MAX_HISTORY = 50
_MAX_RECENT_FILES = 20  # max items per recent list (.opq / .opr)

# Fields that project settings should NOT override (stay global-only)
_GLOBAL_ONLY_FIELDS = frozenset({
    "search_history", "window_width", "window_height", "last_directory",
    "recent_opq", "recent_opr", "editors",
})
_MAX_RECENT_RULES = 30


def _default_config_dir() -> Path:
    """Return the OS-appropriate user config directory for osprey."""
    try:
        from platformdirs import user_config_dir  # type: ignore[import]
        return Path(user_config_dir(_APP_NAME))
    except ImportError:
        # Fallback when platformdirs is not installed
        return Path.home() / f".{_APP_NAME}"


def _project_config_path(work_dir: Path) -> Path:
    """Return the expected project-level config path for *work_dir*."""
    return work_dir / _PROJECT_DIR / _SETTINGS_FILENAME


@dataclasses.dataclass
class EditorConfig:
    """An external editor reachable from the Preview panel context menu.

    The *command* is a template string supporting two placeholders:
    - ``{file}``  — absolute path to the file to open.
    - ``{line}``  — 1-based line number (omit or use ``{file}:{line}`` form).

    Examples::

        code --goto {file}:{line}          # VS Code
        subl {file}:{line}                 # Sublime Text
        notepad++ {file}                   # Notepad++ (no line)
    """

    name: str
    command: str

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "EditorConfig":
        return cls(
            name=str(data.get("name", "")),
            command=str(data.get("command", "")),
        )

    def open_file(self, file: "Path", line: int | None = None) -> None:
        """Launch this editor for *file*, optionally at *line*.

        Strips ``:{line}`` from the command when *line* is unavailable so the
        editor still opens the file without a malformed trailing colon.
        """
        template = self.command
        if line:
            cmd_str = template.replace("{file}", str(file)).replace("{line}", str(line))
        else:
            # Remove common :{line} suffix so the editor isn't passed "file.py:"
            cmd_str = re.sub(r":\{line\}", "", template)
            cmd_str = cmd_str.replace("{file}", str(file)).replace("{line}", "")

        try:
            # posix=False preserves Windows backslash paths inside quoted tokens.
            args = shlex.split(cmd_str, posix=(sys.platform != "win32"))
            logger.info("[Editor] Launching %r: %s", self.name, args)
            subprocess.Popen(args, creationflags=_CREATE_NO_WINDOW)  # noqa: S603 — user-configured command
        except (OSError, ValueError) as exc:
            logger.error("[Editor] Failed to launch %r (%s): %s", self.name, cmd_str, exc)


@dataclasses.dataclass
class SearchBookmark:
    """A saved search configuration the user can recall by name."""

    name: str
    pattern: str
    paths: list[str]
    include_globs: list[str]
    exclude_globs: list[str]
    regex: bool = False
    case_sensitive: bool = True


@dataclasses.dataclass
class SearchProfile:
    """Reusable search configuration stored in a .opq file."""

    pattern: str = ""
    paths: list[str] = dataclasses.field(default_factory=list)
    include_rules: list[str] = dataclasses.field(default_factory=list)
    exclude_rules: list[str] = dataclasses.field(default_factory=list)
    engine_preference: str = "ripgrep"
    regex: bool = False
    case_sensitive: bool = True
    whole_word: bool = False
    use_regex_rules: bool = False

    def to_dict(self) -> dict:
        """Return a JSON-serializable representation."""
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "SearchProfile":
        """Create a profile from serialized data."""
        return cls(
            pattern=str(data.get("pattern", "")),
            paths=[str(path) for path in data.get("paths", [])],
            include_rules=[str(rule) for rule in data.get("include_rules", [])],
            exclude_rules=[str(rule) for rule in data.get("exclude_rules", [])],
            engine_preference=str(data.get("engine_preference", "ripgrep")),
            regex=bool(data.get("regex", False)),
            case_sensitive=bool(data.get("case_sensitive", True)),
            whole_word=bool(data.get("whole_word", False)),
            use_regex_rules=bool(data.get("use_regex_rules", False)),
        )

    def save(self, path: Path) -> None:
        """Save the profile to a .opq file."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "SearchProfile":
        """Load a profile from a .opq file."""
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls.from_dict(data)

    @classmethod
    def from_query(
        cls,
        query: "SearchQuery",
        *,
        engine_preference: str,
    ) -> "SearchProfile":
        """Build a profile from a SearchQuery."""
        return cls(
            pattern=query.pattern,
            paths=[str(path) for path in query.paths],
            include_rules=list(query.file_filter.include_rules),
            exclude_rules=list(query.file_filter.exclude_rules),
            engine_preference=engine_preference,
            regex=query.regex,
            case_sensitive=query.case_sensitive,
            whole_word=query.whole_word,
            use_regex_rules=query.file_filter.use_regex,
        )


@dataclasses.dataclass
class AppSettings:
    """
    Persistent application-level settings.

    Load once at startup, save on exit or when user explicitly saves.
    """

    last_directory: str = str(Path.home())
    engine_preference: str = "ripgrep"
    search_history: list[str] = dataclasses.field(default_factory=list)
    bookmarks: list[SearchBookmark] = dataclasses.field(default_factory=list)
    include_rules: list[str] = dataclasses.field(default_factory=list)
    exclude_rules: list[str] = dataclasses.field(default_factory=list)
    recent_include_rules: list[str] = dataclasses.field(default_factory=list)
    recent_exclude_rules: list[str] = dataclasses.field(default_factory=list)
    exclude_patterns: list[str] = dataclasses.field(
        default_factory=lambda: [".git", "node_modules", "__pycache__", ".venv"]
    )
    window_width: int = 1200
    window_height: int = 800
    show_preview_panel: bool = True
    # Show 1-based line numbers in the Preview panel normal (non-diff) view.
    show_line_numbers: bool = True
    # Tree model backend: False = plain (loads all at once), True = virtual (lazy / chunked)
    use_virtual_model: bool = False
    # MRU lists — up to _MAX_RECENT_FILES absolute paths each (global-only, not project-scoped)
    recent_opq: list[str] = dataclasses.field(default_factory=list)  # recent .opq query profiles
    recent_opr: list[str] = dataclasses.field(default_factory=list)  # recent .opr result files
    # External editor configurations (global-only)
    editors: list[EditorConfig] = dataclasses.field(default_factory=list)

    # ------------------------------------------------------------------
    # Persistence — two-level config
    # ------------------------------------------------------------------

    @classmethod
    def load(cls, config_dir: Path | None = None) -> "AppSettings":
        """
        Load settings using the two-level merge strategy.

        1. Load global config from *config_dir* (defaults to platformdirs path).
        2. Detect project config in the current working directory.
        3. Overlay project fields on top of global settings.

        Returns merged settings, falling back to defaults on any parse error.
        """
        global_settings = cls._load_from_file(
            (config_dir or _default_config_dir()) / _SETTINGS_FILENAME,
            label="global",
        )

        # Probe for project-level config relative to cwd
        project_path = _project_config_path(Path.cwd())
        if project_path.exists():
            project_settings = cls._load_from_file(project_path, label="project")
            if project_settings is not None:
                return cls._merge(global_settings, project_settings)

        return global_settings

    @classmethod
    def load_global(cls, config_dir: Path | None = None) -> "AppSettings":
        """Load only the global config (no project overlay)."""
        return cls._load_from_file(
            (config_dir or _default_config_dir()) / _SETTINGS_FILENAME,
            label="global",
        )

    @classmethod
    def load_for_directory(cls, work_dir: Path, config_dir: Path | None = None) -> "AppSettings":
        """Load global settings and apply project overlay from *work_dir*."""
        base = cls._load_from_file(
            (config_dir or _default_config_dir()) / _SETTINGS_FILENAME,
            label="global",
        )
        project_path = _project_config_path(work_dir)
        if project_path.exists():
            project = cls._load_from_file(project_path, label="project")
            logger.info("[Settings] Project config loaded from %s", project_path)
            return cls._merge(base, project)
        return base

    @classmethod
    def _load_from_file(cls, path: Path, label: str = "") -> "AppSettings":
        """Load a single JSON settings file; return defaults on failure."""
        if not path.exists():
            logger.debug("[Settings] No %s settings at %s; using defaults", label, path)
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            bookmarks = [SearchBookmark(**b) for b in data.pop("bookmarks", [])]
            editors_raw = data.pop("editors", [])
            field_names = {field.name for field in dataclasses.fields(cls)}
            inst = cls(**{k: v for k, v in data.items() if k in field_names})
            inst.bookmarks = bookmarks
            inst.editors = [
                EditorConfig.from_dict(e) for e in editors_raw if isinstance(e, dict)
            ]
            logger.debug("[Settings] Loaded %s settings from %s", label, path)
            return inst
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            logger.warning("[Settings] Failed to load %s settings (%s); using defaults", label, exc)
            return cls()

    @classmethod
    def _merge(cls, base: "AppSettings", project: "AppSettings") -> "AppSettings":
        """
        Apply field-level overlay: project fields override base, except for
        fields listed in _GLOBAL_ONLY_FIELDS which are always kept from base.
        """
        base_dict = dataclasses.asdict(base)
        project_dict = dataclasses.asdict(project)
        project_defaults = dataclasses.asdict(cls())

        merged: dict = dict(base_dict)
        for field in dataclasses.fields(cls):
            field_name = field.name
            if field_name in _GLOBAL_ONLY_FIELDS:
                continue  # always inherit from global
            # Only override if the project value differs from the default
            # (meaning the project explicitly set it)
            if project_dict.get(field_name) != project_defaults.get(field_name):
                merged[field_name] = project_dict[field_name]

        # Bookmarks: union (project bookmarks are added to global)
        extra_bm = [
            SearchBookmark(**b)
            for b in project_dict.get("bookmarks", [])
            if b["name"] not in {bb.name for bb in base.bookmarks}
        ]
        result = cls(**{k: v for k, v in merged.items() if k != "bookmarks"})
        result.bookmarks = base.bookmarks + extra_bm
        return result

    def save(self, config_dir: Path | None = None) -> None:
        """Save to the global config directory."""
        dir_path = config_dir or _default_config_dir()
        self._write_to(dir_path / _SETTINGS_FILENAME)

    def save_project(self, work_dir: Path) -> None:
        """Save to the project-level config directory within *work_dir*."""
        path = _project_config_path(work_dir)
        self._write_to(path)
        logger.info("[Settings] Project settings saved to %s", path)

    def _write_to(self, path: Path) -> None:
        """Persist this settings object to *path* as JSON."""
        path.parent.mkdir(parents=True, exist_ok=True)
        data = dataclasses.asdict(self)
        try:
            path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
            logger.debug("[Settings] Saved to %s", path)
        except OSError as exc:
            logger.error("[Settings] Save failed (%s): %s", path, exc)

    # ------------------------------------------------------------------
    # History helpers
    # ------------------------------------------------------------------

    def push_history(self, pattern: str) -> None:
        """Add pattern to the top of history, deduplicating and capping length."""
        if pattern in self.search_history:
            self.search_history.remove(pattern)
        self.search_history.insert(0, pattern)
        self.search_history = self.search_history[:_MAX_HISTORY]

    def push_recent_opq(self, path: str) -> None:
        """Record a .opq query profile path in the MRU list (deduplicating, max 20)."""
        self._push_recent(self.recent_opq, path)

    def push_recent_opr(self, path: str) -> None:
        """Record a .opr results path in the MRU list (deduplicating, max 20)."""
        self._push_recent(self.recent_opr, path)

    @staticmethod
    def _push_recent(lst: list[str], path: str) -> None:
        """Insert *path* at the front of *lst*, deduplicating and capping at _MAX_RECENT_FILES."""
        norm = str(Path(path).resolve())
        if norm in lst:
            lst.remove(norm)
        lst.insert(0, norm)
        del lst[_MAX_RECENT_FILES:]

    def touch_recent_rule(self, rule: str, *, kind: str) -> None:
        """Record a rule in the recently used rules list."""
        cleaned = rule.strip()
        if not cleaned:
            return

        if kind == "include":
            recent = self.recent_include_rules
        elif kind == "exclude":
            recent = self.recent_exclude_rules
        else:
            raise ValueError(f"Unknown recent rule kind: {kind}")

        if cleaned in recent:
            recent.remove(cleaned)
        recent.insert(0, cleaned)
        del recent[_MAX_RECENT_RULES:]
