"""Runtime entrypoint for long-running Docker/dev processes."""

import argparse
import sys
from pathlib import Path

from .main import DEFAULT_CONFIG_PATH, EXIT_ERROR, cmd_gui, cmd_scheduler


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dk-runtime",
        description="Start Dockkeep runtime processes.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        metavar="PATH",
        help=f"Path to TOML config file (default: {DEFAULT_CONFIG_PATH})",
    )

    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")
    subparsers.required = True

    gui_parser = subparsers.add_parser(
        "gui",
        help="Start the FastAPI GUI runtime and scheduler owner",
    )
    gui_parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="Bind host (default: 0.0.0.0)",
    )
    gui_parser.add_argument(
        "--port",
        type=int,
        default=8080,
        help="Bind port (default: 8080)",
    )

    subparsers.add_parser(
        "scheduler",
        help="Start the headless scheduler runtime (requires DK_MODE=cli)",
    )

    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    config_path: Path = args.config
    if args.command == "gui":
        sys.exit(cmd_gui(args, config_path))
    if args.command == "scheduler":
        sys.exit(cmd_scheduler(config_path))

    parser.print_help()
    sys.exit(EXIT_ERROR)


if __name__ == "__main__":
    main()
