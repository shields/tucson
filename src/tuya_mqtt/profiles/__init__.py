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

from tuya_mqtt.domain import BridgeError, ErrorCode

from .sunbeam import PROFILE as SUNBEAM
from .zemismart import PROFILE as ZEMISMART
lazy from .base import Profile

PROFILES: dict[str, Profile] = {
    "sunbeam-dual": SUNBEAM,
    "zemismart-bcd300fb": ZEMISMART,
}


def get_profile(name: str) -> Profile:
    try:
        return PROFILES[name]
    except KeyError:
        raise BridgeError(
            ErrorCode.INVALID_CONFIG, "unsupported device profile"
        ) from None
