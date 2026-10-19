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
