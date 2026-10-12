import json
from dataclasses import FrozenInstanceError
from typing import Any
lazy from pathlib import Path

import pytest
from pydantic import ValidationError

from tuya_mqtt.config import SecretReference, load_config
from tuya_mqtt.domain import BridgeError, ErrorCode


def test_resolved_config_is_immutable_and_redacted(
    config_path: Path, secret_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SUNBEAM_LOCAL_KEY", secret_env["SUNBEAM_LOCAL_KEY"])
    monkeypatch.setenv("MQTT_PASSWORD", secret_env["MQTT_PASSWORD"])
    result = load_config(config_path)
    assert result.config.controllers[0].protocol_version == "3.3"
    assert result.config.controllers[0].zones == ("side_a", "side_b")
    assert result.local_keys == (secret_env["SUNBEAM_LOCAL_KEY"],)
    assert result.mqtt_password == secret_env["MQTT_PASSWORD"]
    for secret in secret_env.values():
        assert secret not in repr(result)
    with pytest.raises(FrozenInstanceError):
        result.local_keys = ()
    with pytest.raises(ValidationError):
        result.config.mqtt.host = "different"


def test_secret_files_and_no_mqtt_password(
    tmp_path: Path, config_data: dict[str, Any]
) -> None:
    secret_file = tmp_path / "key"
    secret_file.write_text("test-key-16-byte\n", encoding="utf-8")
    config_data["controllers"][0]["local_key"] = {"file": str(secret_file)}
    del config_data["mqtt"]["password"]
    del config_data["http"]
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config_data))
    result = load_config(path, env={})
    assert result.local_keys == ("test-key-16-byte",)
    assert result.mqtt_password is None
    assert result.config.http.bind == "127.0.0.1"


@pytest.mark.parametrize(
    ("section", "key", "value"),
    [
        ("root", "unknown", "private-value"),
        ("root", "controllers", []),
        ("mqtt", "unknown", "private-value"),
        ("mqtt", "port", True),
        ("mqtt", "port", 0),
        ("mqtt", "port", 65536),
        ("mqtt", "host", ""),
        ("mqtt", "client_id", ""),
        ("mqtt", "topic_prefix", "tuya/#"),
        ("mqtt", "topic_prefix", "tuya/+/bed"),
        ("mqtt", "topic_prefix", "/tuya"),
        ("mqtt", "password", {"env": "MISSING"}),
        ("mqtt", "password", {"env": "KEY", "file": "key"}),
        ("mqtt", "password", {}),
        ("mqtt", "tls", {"cert_file": "cert"}),
        ("mqtt", "tls", {"key_file": "key"}),
        ("http", "bind", "invalid"),
        ("controller", "profile", "unrecognized"),
        ("controller", "write_mode", "unrecognized"),
        ("controller", "protocol_version", 3.3),
        ("controller", "protocol_version", "3.6"),
        ("controller", "name", "bridge"),
        ("controller", "name", "bed/side"),
        ("controller", "name", "bed#"),
        ("controller", "ip", "0.0.0.0"),  # noqa: S104
        ("controller", "ip", "224.0.0.1"),
        ("controller", "ip", "mqtt.lan"),
        ("controller", "zones", ["left", "left"]),
        ("controller", "zones", ["availability", "right"]),
        ("controller", "zones", ["left"]),
        ("controller", "local_key", {"file": "/nonexistent/secret"}),
    ],
)
def test_invalid_config_is_sanitized(
    section: str,
    key: str,
    value: object,
    config_data: dict[str, Any],
    tmp_path: Path,
    secret_env: dict[str, str],
) -> None:
    target = (
        config_data
        if section == "root"
        else config_data["controllers"][0]
        if section == "controller"
        else config_data[section]
    )
    target[key] = value
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config_data))
    with pytest.raises(BridgeError) as caught:
        load_config(path, env=secret_env)
    assert caught.value.code == ErrorCode.INVALID_CONFIG
    assert "private-value" not in str(caught.value)
    assert "/nonexistent/secret" not in str(caught.value)
    assert "test-key" not in str(caught.value)


@pytest.mark.parametrize("field", ["name", "device_id", "ip"])
def test_duplicate_controller(
    field: str,
    config_data: dict[str, Any],
    config_path: Path,
    secret_env: dict[str, str],
) -> None:
    original = config_data["controllers"][0]
    duplicate = original | {"name": "other", "device_id": "other", "ip": "192.0.2.11"}
    duplicate[field] = original[field]
    config_data["controllers"].append(duplicate)
    config_path.write_text(json.dumps(config_data))
    with pytest.raises(BridgeError, match="invalid configuration"):
        load_config(config_path, env=secret_env)


@pytest.mark.parametrize("value", ["short", "é" * 16, "a" * 17])
def test_invalid_local_key(
    value: str, config_path: Path, secret_env: dict[str, str]
) -> None:
    secret_env["SUNBEAM_LOCAL_KEY"] = value
    with pytest.raises(BridgeError, match="16 ASCII bytes"):
        load_config(config_path, env=secret_env)


@pytest.mark.parametrize("value", ["", "abc\x00"])
def test_invalid_secret(value: str) -> None:
    with pytest.raises(BridgeError, match="invalid secret"):
        SecretReference(env="KEY").resolve({"KEY": value})


def test_non_utf8_secret(tmp_path: Path) -> None:
    path = tmp_path / "key"
    path.write_bytes(b"\xff")
    with pytest.raises(BridgeError, match="cannot resolve secret"):
        SecretReference(file=str(path)).resolve({})


@pytest.mark.parametrize(
    "payload", [b"\xff", b"{", b"{}", b'{"a":1,"a":2}', b'{"a":NaN}']
)
def test_invalid_config_document(payload: bytes, tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_bytes(payload)
    with pytest.raises(BridgeError, match="invalid configuration"):
        load_config(path, env={})


def test_missing_config(tmp_path: Path) -> None:
    with pytest.raises(BridgeError, match="invalid configuration"):
        load_config(tmp_path / "missing")


def test_valid_tls_and_several_controllers(
    config_data: dict[str, Any], config_path: Path, secret_env: dict[str, str]
) -> None:
    config_data["mqtt"]["tls"] = {
        "ca_file": "ca.pem",
        "cert_file": "cert.pem",
        "key_file": "key.pem",
    }
    config_data["controllers"].append(
        config_data["controllers"][0]
        | {"name": "other", "device_id": "other", "ip": "192.0.2.11"}
    )
    config_path.write_text(json.dumps(config_data))
    assert len(load_config(config_path, env=secret_env).config.controllers) == 2
