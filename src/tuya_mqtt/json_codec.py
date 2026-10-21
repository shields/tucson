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
