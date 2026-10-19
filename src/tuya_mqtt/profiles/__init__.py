from tuya_mqtt.domain import BridgeError, ErrorCode

from .sunbeam import PROFILE
lazy from .base import Profile

PROFILES: dict[str, Profile] = {"sunbeam-dual": PROFILE}


def get_profile(name: str) -> Profile:
    try:
        return PROFILES[name]
    except KeyError:
        raise BridgeError(
            ErrorCode.INVALID_CONFIG, "unsupported device profile"
        ) from None
