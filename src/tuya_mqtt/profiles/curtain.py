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

from uuid import uuid4

from tuya_mqtt.domain import BridgeError, Command, CurtainField, ErrorCode
from tuya_mqtt.json_codec import InvalidJSONError, decode

from .base import MAX_PAYLOAD_BYTES, MAX_REQUEST_ID, MIN_PRINTABLE

MAX_POSITION = 100


def parse_command(payload: bytes, generated_id: str | None = None) -> Command:
    if len(payload) > MAX_PAYLOAD_BYTES:
        raise BridgeError(ErrorCode.INVALID_COMMAND, "command payload too large")
    try:
        data = decode(payload)
    except InvalidJSONError:
        raise BridgeError(ErrorCode.INVALID_COMMAND, "invalid command JSON") from None
    if not isinstance(data, dict) or data.keys() - {
        "request_id",
        "target_position",
    }:
        raise BridgeError(ErrorCode.INVALID_COMMAND, "unknown command fields")
    request_id = data.get("request_id", generated_id or str(uuid4()))
    if (
        not isinstance(request_id, str)
        or not 1 <= len(request_id) <= MAX_REQUEST_ID
        or any(ord(char) < MIN_PRINTABLE for char in request_id)
    ):
        raise BridgeError(ErrorCode.INVALID_COMMAND, "invalid request ID")
    operations = data.keys() - {"request_id"}
    if operations == {"target_position"}:
        value = data["target_position"]
        if type(value) is int and 0 <= value <= MAX_POSITION:
            return Command(request_id, ((CurtainField.TARGET_POSITION, value),))
    raise BridgeError(ErrorCode.INVALID_COMMAND, "invalid curtain command")
