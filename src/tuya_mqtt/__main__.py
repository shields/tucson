import argparse
import sys
from pathlib import Path

from .config import load_config
from .domain import BridgeError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tuya-mqtt")
    subcommands = parser.add_subparsers(dest="command", required=True)
    validate = subcommands.add_parser("validate-config")
    validate.add_argument("--config", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        load_config(args.config)
    except BridgeError as error:
        print(f"{error.code}: {error}", file=sys.stderr)
        return 1
    print("configuration valid")
    return 0


if __name__ == "__main__":
    sys.exit(main())
