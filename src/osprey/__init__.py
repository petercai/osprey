"""Osprey — powerful GUI search and replace tool."""

__version__ = "1.0.0"


def main() -> None:
    """Entry point: parse CLI args, launch GUI or run headless."""
    import sys
    import logging

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    from osprey.__main__ import _build_parser

    parser = _build_parser()
    args = parser.parse_args()

    try:
        if args.headless:
            from osprey.app import run_headless
            sys.exit(run_headless(args))
        else:
            from osprey.app import run_gui
            run_gui(cli_args=args)
    except ImportError:
        print("PySide6 is not installed. Install it with: pip install PySide6")
        print("For CLI usage, run: osprey --help")
        sys.exit(1)
