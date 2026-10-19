from tuya_mqtt.__main__ import main
from tuya_mqtt.profiles import PROFILES

from .curtain_profile import PROFILE

PROFILES["test-curtain"] = PROFILE

if __name__ == "__main__":
    raise SystemExit(main())
