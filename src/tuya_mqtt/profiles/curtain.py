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
