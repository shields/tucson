# Copyright © 2026 Michael Shields
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import argparse
import asyncio
import json
import sys
from pathlib import Path

import structlog

from .config import load_config
from .domain import BridgeError
from .tuya_client import TinyTuyaClient, sanitized_dps
lazy from .runtime import run


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tuya-mqtt")
    subcommands = parser.add_subparsers(dest="command", required=True)
    validate = subcommands.add_parser("validate-config")
    validate.add_argument("--config", type=Path, required=True)
    daemon = subcommands.add_parser("run")
    daemon.add_argument("--config", type=Path, required=True)
    inspect = subcommands.add_parser("inspect")
    inspect.add_argument("--config", type=Path, required=True)
    inspect.add_argument("--controller", required=True)
    args = parser.parse_args(argv)
    try:
        resolved = load_config(args.config)
        if args.command == "run":
            asyncio.run(run(resolved))
            return 0
        if args.command == "inspect":
            for config, key in zip(
                resolved.config.controllers, resolved.local_keys, strict=True
            ):
                if config.name == args.controller:
                    client = TinyTuyaClient(config, key)
                    try:
                        secrets = resolved.local_keys + (
                            (resolved.mqtt_password,)
                            if resolved.mqtt_password is not None
                            else ()
                        )
                        print(json.dumps(sanitized_dps(client.connect(), secrets)))
                    finally:
                        client.close()
                    return 0
            print("unknown controller", file=sys.stderr)
            return 1
    except BridgeError as error:
        print(f"{error.code}: {error}", file=sys.stderr)
        return 1
    # Exception text can contain credentials; fatal diagnostics include only its type.
    except Exception as error:  # noqa: BLE001
        structlog.get_logger().error(
            "fatal", operation=args.command, error_type=type(error).__name__
        )
        return 1
    print("configuration valid")
    return 0


if __name__ == "__main__":
    sys.exit(main())
