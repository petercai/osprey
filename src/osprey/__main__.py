"""Module entry point for `python -m osprey`.

Usage
-----
  python -m osprey                                # launch GUI, defaults search dir to CWD
  python -m osprey /some/project                  # launch GUI, pre-fill search dir
  python -m osprey search.opq                     # launch GUI, load saved query profile
  python -m osprey results.opr                    # launch GUI, load saved results
  python -m osprey any-file.py                    # launch GUI, use file's parent dir
  python -m osprey --pattern "TODO" --path .      # pre-fill pattern + path
  python -m osprey --pattern "TODO" --headless    # headless: print results, no GUI
  python -m osprey --pattern "TODO" --headless --json  # headless JSON output
  python -m osprey --load results.opr             # explicit --load flag still works
  python -m osprey --profile settings.opq         # explicit --profile flag still works

The optional first positional argument (FILE_OR_PATH) is a shortcut:
  .opq file  → load as search query profile  (same as --profile)
  .opr file  → load as search result snapshot (same as --load)
  directory  → set search directory          (same as --path)
  other file → use parent directory          (same as --path <parent>)
  (absent)   → use current working directory (same as --path .)

Explicit named flags always take precedence over the positional argument.
Without --headless all arguments pre-fill the GUI.  Use --help for full list.
"""
from __future__ import annotations

import argparse
import sys


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="osprey",
        description="Osprey — powerful GUI search & replace tool",
    )
    # Optional positional: FILE_OR_PATH shortcut
    # .opq → load profile; .opr → load results; dir → set path; other file → parent dir
    # absent → default to CWD (GUI mode only)
    p.add_argument("target", nargs="?", metavar="FILE_OR_PATH",
                   help="Optional .opq profile, .opr results file, directory, or any file "
                        "(uses its parent directory). Absent: CWD is used as search directory.")
    p.add_argument("-p", "--pattern", metavar="PATTERN",
                   help="Search pattern (required in --headless mode)")
    p.add_argument("-d", "--path", metavar="DIR",
                   help="Search directory (pre-fills path field; required in --headless)")
    p.add_argument("-e", "--engine", metavar="NAME",
                   help="Select search engine (ripgrep | grep)")
    p.add_argument("--include", metavar="GLOB", action="append", default=[],
                   help="Add include rule (repeatable)")
    p.add_argument("--exclude", metavar="GLOB", action="append", default=[],
                   help="Add exclude rule (repeatable)")
    p.add_argument("-r", "--regex", action="store_true",
                   help="Enable regex search mode")
    p.add_argument("-i", "--ignore-case", action="store_true",
                   help="Case-insensitive search (overrides --case-sensitive)")
    p.add_argument("--case-sensitive", action="store_true",
                   help="Force case-sensitive search")
    p.add_argument("-w", "--word", action="store_true",
                   help="Match whole words only")
    p.add_argument("--load", metavar="FILE.opr",
                   help="Load previously saved results file (.opr) on startup (GUI only)")
    p.add_argument("--profile", metavar="FILE.opq",
                   help="Load a saved search profile (.opq) on startup (GUI only)")

    # Headless mode flags (no GUI)
    p.add_argument(
        "--headless",
        action="store_true",
        help="Run without GUI: print search results to stdout and exit. "
             "Requires --pattern. --path defaults to current directory.",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="Output results as JSON (headless mode only)",
    )
    return p


if __name__ == "__main__":
    parser = _build_parser()
    args = parser.parse_args()

    import logging
    logging.basicConfig(
        level=logging.WARNING,  # headless stays quiet; GUI may reconfigure later
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    if args.headless:
        from osprey.app import run_headless
        sys.exit(run_headless(args))
    else:
        logging.getLogger().setLevel(logging.INFO)
        try:
            from osprey.app import run_gui
            run_gui(cli_args=args)
        except Exception as exc:  # noqa: BLE001
            print(f"Fatal error: {exc}", file=sys.stderr)
            sys.exit(2)
        sys.exit(1)
