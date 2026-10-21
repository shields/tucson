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

import asyncio
import ssl
from dataclasses import dataclass
from enum import StrEnum
from time import monotonic
from typing import Protocol, Self
lazy from collections.abc import AsyncIterator
lazy from types import TracebackType

import aiomqtt
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.properties import Properties
from paho.mqtt.subscribeoptions import SubscribeOptions
lazy import paho.mqtt.client as paho
lazy from paho.mqtt.reasoncodes import ReasonCode

from .domain import BridgeError, ErrorCode
lazy from .config import ResolvedConfig

KEEPALIVE = 15
MQTT_TIMEOUT = 2.0
INCOMING_LIMIT = 256
MAX_PACKET_BYTES = 65536


class Operation(StrEnum):
    CONNECT = "connect"
    SUBSCRIBE = "subscribe"
    PUBLISH = "publish"
    RECEIVE = "receive"
    DISCONNECT = "disconnect"


class MQTTFailureError(ConnectionError):
    def __init__(self, message: str, operation: Operation = Operation.CONNECT) -> None:
        self.operation = operation
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class Message:
    topic: str
    payload: bytes
    retained: bool
    received_at: float | None = None


class MQTTConnection(Protocol):
    @property
    def messages(self) -> AsyncIterator[Message]: ...
    async def subscribe(self, topic: str) -> None: ...
    async def publish(
        self, topic: str, payload: str, *, qos: int, retain: bool
    ) -> None: ...
    async def __aenter__(self) -> Self: ...
    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...


class ReceivedMessage(aiomqtt.Message):
    def __init__(self, message: aiomqtt.Message) -> None:
        super().__init__(
            message.topic,
            message.payload,
            message.qos,
            message.retain,
            message.mid,
            message.properties,
        )
        self.received_at = monotonic()


class StrictQueue(asyncio.Queue[aiomqtt.Message]):
    def put_nowait(self, item: aiomqtt.Message) -> None:
        try:
            super().put_nowait(ReceivedMessage(item))
        except asyncio.QueueFull:
            message = "MQTT incoming queue full"
            raise aiomqtt.MqttError(message) from None


class VerifiedClient(aiomqtt.Client):
    def __init__(
        self, resolved: ResolvedConfig, context: ssl.SSLContext | None
    ) -> None:
        config = resolved.config.mqtt
        properties = Properties(PacketTypes.CONNECT)
        properties.SessionExpiryInterval = 0
        properties.MaximumPacketSize = MAX_PACKET_BYTES
        super().__init__(
            hostname=config.host,
            port=config.port,
            identifier=config.client_id,
            username=config.username,
            password=resolved.mqtt_password,
            protocol=aiomqtt.ProtocolVersion.V5,
            clean_start=True,
            properties=properties,
            keepalive=KEEPALIVE,
            timeout=MQTT_TIMEOUT,
            will=aiomqtt.Will(
                f"{config.topic_prefix}/bridge/availability",
                "offline",
                qos=1,
                retain=True,
            ),
            tls_context=context,
            queue_type=StrictQueue,
            max_queued_incoming_messages=INCOMING_LIMIT,
            max_queued_outgoing_messages=1,
            max_inflight_messages=1,
        )
        self._client.connect_timeout = MQTT_TIMEOUT
        self._publication_lock = asyncio.Lock()
        self._publication_error: aiomqtt.MqttError | None = None
        self._closing = False

    def _client_connect(self) -> None:
        try:
            super()._client_connect()
        finally:
            if self._closing:
                # Socket registration callbacks run on the event loop before cleanup.
                self._loop.call_soon_threadsafe(self.close_transport)

    async def publish_checked(
        self, topic: str, payload: str, *, qos: int, retain: bool
    ) -> None:
        async with self._publication_lock:
            self.check_connection()
            self._publication_error = None
            await super().publish(topic, payload, qos=qos, retain=retain)
            if self._publication_error is not None:
                raise self._publication_error

    def _on_publish(
        self,
        client: paho.Client,
        userdata: object,
        mid: int,
        reason_code: ReasonCode,
        properties: Properties,
    ) -> None:
        # aiomqtt drops the reason code; a denied PUBACK must fail the connection.
        if reason_code.is_failure:
            error = aiomqtt.MqttCodeError(reason_code, "MQTT publication rejected")
            self._publication_error = error
            self._fail(error)
        super()._on_publish(client, userdata, mid, reason_code, properties)

    def _on_message(
        self, client: paho.Client, userdata: object, message: paho.MQTTMessage
    ) -> None:
        try:
            super()._on_message(client, userdata, message)
        except aiomqtt.MqttError as error:
            self._fail(error)

    def _fail(self, error: aiomqtt.MqttError) -> None:
        self._closing = True
        # Paho holds its callback mutex until this callback returns.
        self._loop.call_soon(self.close_transport)
        if not self._disconnected.done():
            self._disconnected.set_exception(error)

    def check_connection(self) -> None:
        if self._closing or self._disconnected.done():
            message = "MQTT connection closed"
            raise aiomqtt.MqttError(message)

    def abort_transport(self) -> None:
        self._fail(aiomqtt.MqttError("MQTT session aborted"))
        self.close_transport()

    def close_transport(self) -> None:
        self._closing = True
        # Callback failures can complete aiomqtt's future with the socket still open.
        self._client._sock_close()  # noqa: SLF001
        if self._disconnected.done() and not self._disconnected.cancelled():
            self._disconnected.exception()


def tls_context(resolved: ResolvedConfig) -> ssl.SSLContext | None:
    tls = resolved.config.mqtt.tls
    if tls is None:
        return None
    try:
        context = ssl.create_default_context(cafile=tls.ca_file)
        if tls.cert_file is not None:
            context.load_cert_chain(tls.cert_file, tls.key_file)
    except OSError, ssl.SSLError:
        raise BridgeError(
            ErrorCode.INVALID_CONFIG, "invalid TLS configuration"
        ) from None
    return context


class Client:
    def __init__(
        self, resolved: ResolvedConfig, context: ssl.SSLContext | None
    ) -> None:
        self._client = VerifiedClient(resolved, context)

    async def __aenter__(self) -> Self:
        try:
            await self._client.__aenter__()
            self._client.check_connection()
        except (aiomqtt.MqttError, asyncio.CancelledError) as error:
            self._client.close_transport()
            if isinstance(error, asyncio.CancelledError):
                raise
            message = "MQTT connection failed"
            raise MQTTFailureError(message) from None
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        try:
            if exc is not None:
                self._client.abort_transport()
            await self._client.__aexit__(exc_type, exc, traceback)
        except aiomqtt.MqttError:
            message = "MQTT disconnect failed"
            if exc is not None:
                raise BaseExceptionGroup(
                    message, [exc, MQTTFailureError(message, Operation.DISCONNECT)]
                ) from None
            raise MQTTFailureError(message, Operation.DISCONNECT) from None
        finally:
            self._client.close_transport()

    async def subscribe(self, topic: str) -> None:
        try:
            self._client.check_connection()
            reasons = await self._client.subscribe(
                topic,
                options=SubscribeOptions(
                    qos=0, retainHandling=2, retainAsPublished=True
                ),
            )
            if len(reasons) != 1 or reasons[0] != 0:
                message = "MQTT subscription rejected"
                raise MQTTFailureError(message, Operation.SUBSCRIBE)
        except aiomqtt.MqttError:
            message = "MQTT subscription failed"
            raise MQTTFailureError(message, Operation.SUBSCRIBE) from None

    async def publish(
        self, topic: str, payload: str, *, qos: int, retain: bool
    ) -> None:
        try:
            await self._client.publish_checked(topic, payload, qos=qos, retain=retain)
        except aiomqtt.MqttError:
            message = "MQTT publication failed"
            raise MQTTFailureError(message, Operation.PUBLISH) from None

    @property
    def messages(self) -> AsyncIterator[Message]:
        return self._messages()

    async def _messages(self) -> AsyncIterator[Message]:
        try:
            async for message in self._client.messages:
                self._client.check_connection()
                yield Message(
                    str(message.topic),
                    bytes(message.payload or b""),
                    message.retain,
                    message.received_at
                    if isinstance(message, ReceivedMessage)
                    else None,
                )
        except aiomqtt.MqttError:
            message = "MQTT message stream failed"
            raise MQTTFailureError(message, Operation.RECEIVE) from None
