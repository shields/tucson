import asyncio
import base64
import hashlib
import os
import re
from contextlib import suppress
from pathlib import Path
from typing import Any
lazy from collections.abc import Iterator

import aiomqtt
import pytest

from .broker import Broker
from .process import Observed, finish, ready
from .test_process import configure_process, http_snapshot
lazy from .tcp import Simulator

pytestmark = pytest.mark.integration

CLIENTS = {"hoboken": "hoboken-credential", "observer": "observer-credential"}


def last(data: Observed, topic: str) -> object:
    message = data.latest.get(topic)
    return None if message is None else message.payload


def documented_acl() -> str:
    text = Path("docs/deployment.md").read_text(encoding="utf-8")
    section = text.split("## Broker access control", 1)[1]
    match = re.search(r"```text\n(.*?)```", section, re.DOTALL)
    assert match is not None, "docs/deployment.md must document the ACL"
    return match.group(1)


def password_entry(user: str, password: str) -> str:
    salt = os.urandom(12)
    digest = hashlib.pbkdf2_hmac("sha512", password.encode(), salt, 101)
    encoded = (base64.b64encode(value).decode() for value in (salt, digest))
    return f"{user}:$7$101${'$'.join(encoded)}\n"


@pytest.fixture
def acl_broker(tmp_path: Path, secret_env: dict[str, str]) -> Iterator[Broker]:
    passwords = tmp_path / "passwords"
    passwords.write_text(
        "".join(
            password_entry(user, password)
            for user, password in (
                ("tucson", secret_env["MQTT_PASSWORD"]),
                *CLIENTS.items(),
            )
        ),
        encoding="utf-8",
    )
    acl = tmp_path / "acl"
    acl.write_text(
        f"{documented_acl()}\nuser observer\ntopic readwrite #\n", encoding="utf-8"
    )
    passwords.chmod(0o600)
    acl.chmod(0o600)
    service = Broker(
        tmp_path,
        extra=f"allow_anonymous false\npassword_file {passwords}\nacl_file {acl}\n",
    )
    try:
        service.start()
        yield service
    finally:
        service.stop()


def client(broker: Broker, user: str) -> aiomqtt.Client:
    return aiomqtt.Client(
        "127.0.0.1",
        broker.port,
        username=user,
        password=CLIENTS[user],
        protocol=aiomqtt.ProtocolVersion.V5,
    )


def hoboken_topics() -> list[str]:
    stanza = documented_acl().split("user hoboken\n", 1)[1].split("\nuser ", 1)[0]
    return re.findall(r"^topic read (\S+)$", stanza, re.MULTILINE)


@pytest.mark.parametrize("simulator", ["3.3"], indirect=True)
async def test_daemon_and_hoboken_work_under_the_documented_acl(
    acl_broker: Broker,
    simulator: Simulator,
    config_path: Path,
    config_data: dict[str, Any],
    secret_env: dict[str, str],
) -> None:
    await acl_broker.ready()
    daemon = configure_process(
        config_path, config_data, secret_env, acl_broker, simulator
    )
    observed = Observed()
    hoboken = Observed()
    async with (
        client(acl_broker, "observer") as observer,
        client(acl_broker, "hoboken") as heated_bedding,
    ):
        await observer.subscribe("tuya/#", qos=1)
        for topic in hoboken_topics():
            await heated_bedding.subscribe(topic, qos=1)
        consume = asyncio.create_task(observed.consume(observer))
        consume_hoboken = asyncio.create_task(hoboken.consume(heated_bedding))
        try:
            daemon.start()
            await ready(observed)
            await hoboken.wait(
                lambda data: (
                    "tuya/bridge/heartbeat" in data.latest
                    and "tuya/bed/availability" in data.latest
                    and "tuya/bed/side_a" in data.latest
                    and "tuya/bed/side_b" in data.latest
                )
            )
            await heated_bedding.publish(
                "tuya/bed/side_a/set",
                '{"request_id":"h1","power":true,"heat_level":4}',
                qos=0,
            )
            await hoboken.wait(
                lambda data: (
                    "tuya/bed/side_a/result" in data.latest
                    and data.payload("tuya/bed/side_a/result")["request_id"] == "h1"
                )
            )
            assert hoboken.payload("tuya/bed/side_a/result")["outcome"] == "confirmed"
            assert simulator.writes == [{"14": True, "20": "level_4"}]
            refreshes = observed.count("tuya/bed/side_a")
            await heated_bedding.publish("tuya/bed/side_a/get", b"", qos=0)
            await heated_bedding.publish(
                "tuya/bed/side_a", b"not json", qos=0, retain=True
            )
            await asyncio.sleep(0.5)
            assert observed.count("tuya/bed/side_a") == refreshes
            assert observed.latest["tuya/bed/side_a"].payload != b"not json"
            await observer.publish("tuya/bed/side_a/get", b"", qos=0)
            await observed.wait(lambda data: data.count("tuya/bed/side_a") > refreshes)
            await heated_bedding.publish(
                "tuya/bed/side_b/set",
                '{"request_id":"h2","power":true,"heat_level":2}',
                qos=0,
            )
            await hoboken.wait(
                lambda data: (
                    "tuya/bed/side_b/result" in data.latest
                    and data.payload("tuya/bed/side_b/result")["request_id"] == "h2"
                )
            )
            assert hoboken.payload("tuya/bed/side_b/result")["outcome"] == "confirmed"
            refreshes = observed.count("tuya/bed/side_b")
            await observer.publish("tuya/bed/side_b/get", b"", qos=0)
            await observed.wait(lambda data: data.count("tuya/bed/side_b") > refreshes)
            snapshot = await http_snapshot(config_data)
            assert snapshot["readiness"] == (200, {"ready": True, "reasons": []})
            for counter in ("mqtt_failures", "mqtt_publish_failures"):
                assert f"tuya_mqtt_{counter}_total 0.0" in snapshot["metrics"]
            assert await daemon.stop() == 0
            await observed.wait(
                lambda data: (
                    last(data, "tuya/bridge/availability") == b"offline"
                    and last(data, "tuya/bed/availability") == b"offline"
                )
            )
        finally:
            consume_hoboken.cancel()
            with suppress(asyncio.CancelledError):
                await consume_hoboken
            await finish(daemon, consume)


@pytest.mark.parametrize("simulator", ["3.3"], indirect=True)
async def test_will_is_delivered_under_the_documented_acl(
    acl_broker: Broker,
    simulator: Simulator,
    config_path: Path,
    config_data: dict[str, Any],
    secret_env: dict[str, str],
) -> None:
    await acl_broker.ready()
    daemon = configure_process(
        config_path, config_data, secret_env, acl_broker, simulator
    )
    observed = Observed()
    async with client(acl_broker, "observer") as observer:
        await observer.subscribe("tuya/#", qos=1)
        consume = asyncio.create_task(observed.consume(observer))
        try:
            daemon.start()
            await ready(observed)
            await daemon.stop(abrupt=True)
            await observed.wait(
                lambda data: last(data, "tuya/bridge/availability") == b"offline"
            )
        finally:
            await finish(daemon, consume)
