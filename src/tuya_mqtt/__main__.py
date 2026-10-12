import argparse
import json
import sys
from pathlib import Path

from .config import load_config
from .domain import BridgeError
from .tuya_client import TinyTuyaClient, sanitized_dps


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tuya-mqtt")
    subcommands = parser.add_subparsers(dest="command", required=True)
    validate = subcommands.add_parser("validate-config")
    validate.add_argument("--config", type=Path, required=True)
    inspect = subcommands.add_parser("inspect")
    inspect.add_argument("--config", type=Path, required=True)
    inspect.add_argument("--controller", required=True)
    args = parser.parse_args(argv)
    try:
        resolved = load_config(args.config)
        if args.command == "inspect":
            for config, key in zip(
                resolved.config.controllers, resolved.local_keys, strict=True
            ):
                if config.name == args.controller:
                    client = TinyTuyaClient(config, key)
                    try:
                        secrets = (key, resolved.mqtt_password or key)
                        print(json.dumps(sanitized_dps(client.connect(), secrets)))
                    finally:
                        client.close()
                    return 0
            print("unknown controller", file=sys.stderr)
            return 1
    except BridgeError as error:
        print(f"{error.code}: {error}", file=sys.stderr)
        return 1
    print("configuration valid")
    return 0


if __name__ == "__main__":
    sys.exit(main())
