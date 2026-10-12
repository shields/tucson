import json


class InvalidJSONError(ValueError):
    pass


def _object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            message = "duplicate JSON key"
            raise InvalidJSONError(message)
        result[key] = value
    return result


def _constant(_value: str) -> object:
    message = "nonfinite JSON number"
    raise InvalidJSONError(message)


def decode(payload: str | bytes) -> object:
    try:
        return json.loads(payload, object_pairs_hook=_object, parse_constant=_constant)
    except ValueError, UnicodeError, RecursionError:
        message = "invalid JSON"
        raise InvalidJSONError(message) from None
