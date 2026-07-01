"""
osprey.app
~~~~~~~~~~
PySide6 application bootstrap. Wires up the main window and enters the Qt event loop.
Also provides run_headless() for --headless CLI mode (no GUI).
"""

from __future__ import annotations

import dataclasses
import json
import sys
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class _TargetResolution:
    """Immutable result of resolving the optional positional CLI target argument."""

    path: Path | None = None        # search directory to pre-fill
    profile: str | None = None      # .opq file path to load
    snapshot: str | None = None     # .opr file path to load
    source: str = "none"            # diagnostic label for logging


def _resolve_target(target: str | None, args: Any) -> _TargetResolution:
    """Resolve the optional positional CLI target into a typed resolution.

    Priority: explicit named flag (--path / --profile / --load) > positional target.
    When no target and no --path, defaults to CWD (GUI mode only; caller decides).
    """
    named_path: str | None = getattr(args, "path", None)
    named_profile: str | None = getattr(args, "profile", None)
    named_load: str | None = getattr(args, "load", None)

    if target is None:
        # No positional arg: caller will apply CWD default if appropriate
        return _TargetResolution(source="none")

    t = Path(target)

    if not t.exists():
        logger.warning("[App] CLI target '%s' does not exist, skipping", target)
        return _TargetResolution(source="none")

    suffix = t.suffix.lower()

    if t.is_file() and suffix == ".opq":
        if named_profile:
            logger.warning(
                "[App] --profile overrides positional target %s", target
            )
            return _TargetResolution(source="named:profile")
        return _TargetResolution(profile=str(t.resolve()), source="target:opq")

    if t.is_file() and suffix == ".opr":
        if named_load:
            logger.warning(
                "[App] --load overrides positional target %s", target
            )
            return _TargetResolution(source="named:load")
        return _TargetResolution(snapshot=str(t.resolve()), source="target:opr")

    if t.is_dir():
        if named_path:
            logger.warning(
                "[App] --path overrides positional target %s", target
            )
            return _TargetResolution(source="named:path")
        return _TargetResolution(path=t.resolve(), source="target:dir")

    # Any other file: use its parent directory as search path
    if t.is_file():
        if named_path:
            logger.warning(
                "[App] --path overrides positional target %s", target
            )
            return _TargetResolution(source="named:path")
        return _TargetResolution(path=t.parent.resolve(), source="target:file")

    logger.warning("[App] CLI target '%s' is neither a file nor a directory, skipping", target)
    return _TargetResolution(source="none")


def run_headless(args: Any) -> int:
    """Execute search without GUI; print results to stdout.

    Returns exit code:
      0 — one or more matches found
      1 — no matches (grep convention)
      2 — argument error or engine error

    Output format (default): ``file:line:matched_line``
    With ``--json``: a single JSON object on stdout.
    """
    from osprey.engine.registry import EngineRegistry
    from osprey.engine.base import FileFilter, SearchQuery

    if not getattr(args, "pattern", None):
        print("Error: --pattern is required in headless mode", file=sys.stderr)
        return 2

    registry = EngineRegistry()
    engine_name = getattr(args, "engine", None)
    engine = registry.get_engine(engine_name) if engine_name else None
    if engine is None:
        try:
            engine = registry.best_engine()
        except RuntimeError:
            print("Error: No search engine available (install rg, ag, or grep)", file=sys.stderr)
            return 2

    # Resolve search path: explicit --path wins, then positional target (dir only), then CWD
    if getattr(args, "path", None):
        search_path = Path(args.path).resolve()
    else:
        resolution = _resolve_target(getattr(args, "target", None), args)
        if resolution.path is not None:
            search_path = resolution.path
            logger.info("[headless] path from positional target (%s): %s", resolution.source, search_path)
        else:
            search_path = Path(".").resolve()
    case_sensitive = True
    if getattr(args, "ignore_case", False):
        case_sensitive = False
    elif getattr(args, "case_sensitive", False):
        case_sensitive = True

    query = SearchQuery(
        pattern=args.pattern,
        paths=[search_path],
        file_filter=FileFilter(
            include_rules=list(getattr(args, "include", []) or []),
            exclude_rules=list(getattr(args, "exclude", []) or []),
            use_regex=False,
        ),
        regex=getattr(args, "regex", False),
        case_sensitive=case_sensitive,
        whole_word=getattr(args, "word", False),
    )

    logger.debug(
        "[headless] pattern=%r path=%s engine=%s", args.pattern, search_path, engine.name()
    )

    try:
        result = engine.search(query)
    except Exception as exc:  # noqa: BLE001
        print(f"Error: search failed — {exc}", file=sys.stderr)
        return 2

    if not result.files:
        if getattr(args, "json", False):
            print(json.dumps({"pattern": args.pattern, "total_files": 0, "total_matches": 0, "files": []}))
        return 1  # no matches — grep convention

    if getattr(args, "json", False):
        output = {
            "pattern": result.query.pattern,
            "engine": result.engine_used,
            "elapsed_ms": round(result.elapsed_ms, 2),
            "total_files": result.total_file_count,
            "total_matches": result.total_match_count,
            "files": [
                {
                    "path": str(fm.file_path),
                    "matches": [
                        {
                            "line": m.line_number,
                            "col": m.column_start,
                            "text": m.line_text.rstrip(),
                        }
                        for m in fm.matches
                    ],
                }
                for fm in result.files
            ],
        }
        print(json.dumps(output, ensure_ascii=False))
    else:
        # grep-like format: file:line:matched_line
        for fm in result.files:
            for match in fm.matches:
                print(f"{fm.file_path}:{match.line_number}:{match.line_text.rstrip()}")

    return 0


def _set_macos_display_name(name: str) -> None:
    """Set the macOS process name shown in the global menu bar and Cmd+Tab switcher.

    MUST be called **before** ``QApplication(sys.argv)`` so that NSApplication
    reads the correct process name when it initialises.  Calling it after
    QApplication creation is too late — NSApp will have already captured
    "Python3" from the process at startup.

    Uses ``NSProcessInfo.setProcessName:`` which is the safe, documented API
    for renaming an unpackaged Python process.  NSBundle KVC is intentionally
    NOT used here — NSBundle does not expose ``CFBundleName`` as a KVC key and
    will raise ``NSUnknownKeyException`` if ``setValue:forKey:`` is attempted.

    Tries PyObjC (Foundation) first; falls back to ctypes Objective-C runtime.
    No-op on non-macOS or when all methods fail.
    """
    # -- PyObjC path (pyobjc-framework-Foundation) ----------------------------------
    try:
        from Foundation import NSProcessInfo  # type: ignore[import]
        NSProcessInfo.processInfo().setProcessName_(name)
        logger.debug("[App] macOS process name set to %r via PyObjC", name)
        return
    except ImportError:
        pass
    except Exception as exc:
        logger.debug("[App] PyObjC setProcessName failed: %s", exc)

    # -- ctypes path (no extra dependency, uses macOS system libobjc) ---------------
    try:
        import ctypes, ctypes.util  # noqa: E401

        objc = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc") or "libobjc.dylib")
        objc.objc_getClass.restype = ctypes.c_void_p
        objc.sel_registerName.restype = ctypes.c_void_p

        # [NSProcessInfo processInfo]
        NSProcessInfo_cls = objc.objc_getClass(b"NSProcessInfo")
        objc.objc_msgSend.restype = ctypes.c_void_p
        objc.objc_msgSend.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        process_info = objc.objc_msgSend(NSProcessInfo_cls, objc.sel_registerName(b"processInfo"))

        # NSString for the new name
        NSString_cls = objc.objc_getClass(b"NSString")
        objc.objc_msgSend.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_char_p]
        objc.objc_msgSend.restype = ctypes.c_void_p
        ns_name = objc.objc_msgSend(
            NSString_cls, objc.sel_registerName(b"stringWithUTF8String:"), name.encode("utf-8")
        )

        # [processInfo setProcessName:ns_name]
        objc.objc_msgSend.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        objc.objc_msgSend.restype = None
        objc.objc_msgSend(process_info, objc.sel_registerName(b"setProcessName:"), ns_name)

        logger.debug("[App] macOS process name set to %r via ctypes", name)
    except Exception as exc:
        logger.debug("[App] ctypes setProcessName failed: %s", exc)


def _set_macos_bundle_id(bundle_id: str) -> None:
    """Inject *bundle_id* into the main bundle's Info.plist dictionary.

    An unpackaged Python process has no ``CFBundleIdentifier``, which causes
    IMKit to log "error messaging the mach port for IMKCFRunLoopWakeUpReliable"
    repeatedly in the terminal.  Setting the identifier before NSApplication
    activates gives IMKit a valid token and suppresses the warning.

    SAFE approach: mutate ``[NSBundle mainBundle] infoDictionary`` directly.
    This is NOT the same as ``NSBundle setValue:forKey:`` (KVC) which crashes
    with ``NSUnknownKeyException`` — see iteration 20c notes in the design doc.

    Tries PyObjC first; falls back to ctypes Objective-C runtime.
    No-op when all methods fail (warning will still appear but app is unaffected).
    """
    # -- PyObjC path ---------------------------------------------------------------
    try:
        from Foundation import NSBundle  # type: ignore[import]
        info = NSBundle.mainBundle().infoDictionary()
        if info is not None and not info.get("CFBundleIdentifier"):
            info["CFBundleIdentifier"] = bundle_id
            logger.debug("[App] CFBundleIdentifier set to %r via PyObjC", bundle_id)
        return
    except ImportError:
        pass
    except Exception as exc:
        logger.debug("[App] PyObjC CFBundleIdentifier failed: %s", exc)

    # -- ctypes path ---------------------------------------------------------------
    try:
        import ctypes, ctypes.util  # noqa: E401

        objc = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc") or "libobjc.dylib")
        objc.objc_getClass.restype = ctypes.c_void_p
        objc.sel_registerName.restype = ctypes.c_void_p

        # Helpers: build NSString from bytes
        NSString_cls = objc.objc_getClass(b"NSString")

        def _ns_str(s: str) -> ctypes.c_void_p:
            objc.objc_msgSend.restype = ctypes.c_void_p
            objc.objc_msgSend.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_char_p]
            return objc.objc_msgSend(
                NSString_cls,
                objc.sel_registerName(b"stringWithUTF8String:"),
                s.encode("utf-8"),
            )

        # [NSBundle mainBundle]
        NSBundle_cls = objc.objc_getClass(b"NSBundle")
        objc.objc_msgSend.restype = ctypes.c_void_p
        objc.objc_msgSend.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        main_bundle = objc.objc_msgSend(NSBundle_cls, objc.sel_registerName(b"mainBundle"))

        # [mainBundle infoDictionary]
        info_dict = objc.objc_msgSend(main_bundle, objc.sel_registerName(b"infoDictionary"))

        # Check whether CFBundleIdentifier is already set (objectForKey:)
        key_ns = _ns_str("CFBundleIdentifier")
        objc.objc_msgSend.restype = ctypes.c_void_p
        objc.objc_msgSend.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        existing = objc.objc_msgSend(info_dict, objc.sel_registerName(b"objectForKey:"), key_ns)
        if existing:
            logger.debug("[App] CFBundleIdentifier already set — skipping ctypes path")
            return

        # [infoDict setObject:val forKey:key]
        val_ns = _ns_str(bundle_id)
        objc.objc_msgSend.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p
        ]
        objc.objc_msgSend.restype = None
        objc.objc_msgSend(
            info_dict, objc.sel_registerName(b"setObject:forKey:"), val_ns, key_ns
        )
        logger.debug("[App] CFBundleIdentifier set to %r via ctypes", bundle_id)
    except Exception as exc:
        logger.debug("[App] ctypes CFBundleIdentifier failed: %s", exc)


def run_gui(cli_args: Any = None) -> None:
    """Create QApplication, show MainWindow, and enter the event loop.

    Parameters
    ----------
    cli_args:
        Parsed ``argparse.Namespace`` from ``__main__``, or *None* when
        launched from the console entry-point without arguments.
    """
    # macOS: MUST be done before QApplication / NSApplication initialises.
    # Once NSApp has started it has already captured "Python3" from the bundle;
    # setting the name afterwards only changes diagnostic strings, not the UI.
    if sys.platform == "darwin":
        _set_macos_display_name("Osprey")
        # Suppress "error messaging the mach port for IMKCFRunLoopWakeUpReliable":
        # IMKit requires a valid CFBundleIdentifier to set up its mach port.
        # Mutating infoDictionary is safe; NSBundle KVC (setValue:forKey:) is NOT.
        _set_macos_bundle_id("com.github.osprey")

    from PySide6.QtWidgets import QApplication
    from PySide6.QtGui import QIcon
    from osprey.ui.main_window import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName("Osprey")
    app.setApplicationDisplayName("Osprey")
    app.setApplicationVersion("1.0.0")
    app.setOrganizationName("osprey")

    # Set application icon; resolve relative to the project root at runtime.
    _icon_path = Path(__file__).parent.parent.parent / "icon-osprey.png"
    if _icon_path.exists():
        app.setWindowIcon(QIcon(str(_icon_path)))
        logger.debug("[App] Icon loaded from %s", _icon_path)
    else:
        logger.debug("[App] icon-osprey.png not found at %s — skipping", _icon_path)

    window = MainWindow()

    # Apply CLI arguments to pre-fill GUI state before showing the window
    if cli_args is not None:
        _apply_cli_args(window, cli_args)

    window.show()

    logger.info("[App] Osprey started")
    sys.exit(app.exec())


def _apply_cli_args(window: Any, args: Any) -> None:
    """Pre-fill the MainWindow GUI from parsed CLI arguments."""
    sp = window._search_panel  # direct access intentional — app bootstrap only

    # --- Positional target resolution (named args take precedence) ---
    resolution = _resolve_target(getattr(args, "target", None), args)

    if resolution.source not in ("none", "named:path", "named:profile", "named:load"):
        # Apply target-derived value only when the named arg did not override it
        if resolution.path is not None and not getattr(args, "path", None):
            sp.set_directory(str(resolution.path))
            logger.info("[App] CLI pre-fill: path from positional target (%s): %s",
                        resolution.source, resolution.path)
        if resolution.profile is not None and not getattr(args, "profile", None):
            window._load_search_options_from_path(resolution.profile)
            logger.info("[App] CLI pre-fill: profile from positional target: %s", resolution.profile)
        if resolution.snapshot is not None and not getattr(args, "load", None):
            window._load_results_from_path(resolution.snapshot)
            logger.info("[App] CLI pre-fill: snapshot from positional target: %s", resolution.snapshot)

    # --- Default to CWD when no path source was given ---
    if (resolution.path is None
            and resolution.profile is None
            and resolution.snapshot is None
            and not getattr(args, "path", None)):
        cwd = str(Path.cwd())
        sp.set_directory(cwd)
        logger.info("[App] CLI pre-fill: default CWD: %s", cwd)

    # --- Named args (always applied; override positional where applicable) ---
    if getattr(args, "path", None):
        sp.set_directory(args.path)
        logger.info("[App] CLI pre-fill: path=%s", args.path)

    if getattr(args, "pattern", None):
        sp.set_pattern(args.pattern)
        logger.info("[App] CLI pre-fill: pattern=%r", args.pattern)

    if getattr(args, "engine", None):
        sp.set_engine_preference(args.engine)
        logger.info("[App] CLI pre-fill: engine=%s", args.engine)

    for rule in getattr(args, "include", []) or []:
        sp.add_include_rule(rule)
    for rule in getattr(args, "exclude", []) or []:
        sp.add_exclude_rule(rule)

    if getattr(args, "regex", False):
        sp.set_regex(True)
    if getattr(args, "word", False):
        sp.set_whole_word(True)
    if getattr(args, "ignore_case", False):
        sp.set_case_sensitive(False)
    elif getattr(args, "case_sensitive", False):
        sp.set_case_sensitive(True)

    # Load a saved profile (.opq) or results snapshot (.opr) on startup
    if getattr(args, "profile", None):
        window._load_search_options_from_path(args.profile)
    if getattr(args, "load", None):
        window._load_results_from_path(args.load)
