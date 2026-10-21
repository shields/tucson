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

import logging
import sys
lazy from typing import TextIO

import structlog
lazy from structlog.types import EventDict, WrappedLogger


class UnsafeLogValueError(TypeError):
    pass


def _primitive(value: object, secrets: tuple[str, ...]) -> object:
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, "[redacted]")
        return value
    if value is None or type(value) in (bool, int, float):
        return value
    if isinstance(value, list | tuple):
        return [_primitive(item, secrets) for item in value]
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return {
            str(_primitive(key, secrets)): _primitive(item, secrets)
            for key, item in value.items()
        }
    message = "unsupported structured log value"
    raise UnsafeLogValueError(message)


def configure(secrets: tuple[str, ...], *, stream: TextIO | None = None) -> None:
    ordered = tuple(sorted(set(secrets), key=len, reverse=True))

    def redact(_logger: WrappedLogger, _method: str, event: EventDict) -> EventDict:
        return {
            str(_primitive(key, ordered)): _primitive(value, ordered)
            for key, value in event.items()
        }

    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            redact,
            structlog.processors.JSONRenderer(allow_nan=False),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        logger_factory=structlog.WriteLoggerFactory(file=stream or sys.stdout),
        cache_logger_on_first_use=True,
    )


def sanitize(value: object, secrets: tuple[str, ...]) -> object:
    return _primitive(value, tuple(sorted(set(secrets), key=len, reverse=True)))
