import logging
import os
import uuid
from asyncio import Future, get_event_loop
from typing import Any
from urllib.parse import urlparse

from tornado.websocket import WebSocketHandler as TornadoWebSocketHandler

from py_app_runner.bridge.encoders import JsonEncoder, MessageEncoder, MsgpackEncoder
from py_app_runner.http_exception import HTTPException
from py_app_runner.request_handler.handlers import RequestHandlerApiKeys
from py_app_runner.return_model import MessageModel, ReturnModel, StatusModel
from py_app_runner.utils import json_encode
from py_app_runner.wbcm.ws_interface import WebSocketHandlerInterface

_ws_allowed_origins_raw = os.environ.get("WS_ALLOWED_ORIGINS", "")
WS_ALLOWED_ORIGINS: set[str] = {o.strip().lower().rstrip("/") for o in _ws_allowed_origins_raw.split(",") if o.strip()}


# * BaseWebSocketHandler - protocol-agnostic WebSocket handler
class BaseWebSocketHandler(RequestHandlerApiKeys, TornadoWebSocketHandler, WebSocketHandlerInterface):
    """Base WebSocket request handler with pluggable encoding."""

    encoder: MessageEncoder

    #########################
    ### Class lifecycle #####
    #########################

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        logger_name = f"{__name__}.{self.__class__.__name__}"
        self.logger = logging.getLogger(logger_name)

        self.loop = get_event_loop()

        self.uid = None
        self.msg_id = None
        self.service = None
        self.device_id: str | None = None
        self._api_key_valid: bool | None = None

        super().__init__(*args, **kwargs)

    ###############
    ### Helpers ###
    ###############

    def error_message(self, msg: str | HTTPException, code: int = -1, **kwargs: Any):
        error: dict[str, Any]
        if isinstance(msg, HTTPException):
            error = msg.to_dict()
        else:
            error = {"code": code, "msg": msg}

        message: dict[str, Any] = {"error": error}
        message.update(kwargs)
        self.write_custom_message(message)

    #################
    ### Overrides ###
    #################

    def write_custom_message(
        self,
        message: bytes | str | dict[str, Any] | list[Any],
        wrap_in_data: bool = True,
        **kwargs: Any,
    ) -> Future[None]:
        if isinstance(message, (StatusModel, MessageModel, ReturnModel)):
            message = message.to_dict()
        if wrap_in_data:
            message = {"data": message}

        response_message: str | bytes = b""
        if type(message) is dict:
            # Add service
            if self.service and "service" not in message:
                message["service"] = self.service

            # Add msg_id
            if self.msg_id and "msg_id" not in message:
                message["msg_id"] = self.msg_id

            # Add additional fields
            message.update(kwargs)

            # Encode message using the pluggable encoder
            response_message = self.encoder.encode(message)

        if self.ws_connection is not None and self.ws_connection.is_closing() is False:
            return super().write_message(response_message, binary=self.encoder.is_binary)  # type: ignore

        return Future()

    ########################
    ### Request handling ###
    ########################

    async def reload_token(self, new_auth_token: str) -> None:
        old_auth_token = self.auth_token
        if old_auth_token != new_auth_token:
            self.auth_token = new_auth_token
            await self._ensure_current_user()

    def check_origin(self, origin: str) -> bool:
        if not WS_ALLOWED_ORIGINS:
            return True

        normalized = origin.lower().rstrip("/")
        if normalized in WS_ALLOWED_ORIGINS:
            return True

        # Also check just the scheme + host (ignoring path)
        parsed = urlparse(normalized)
        origin_host = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else normalized
        return origin_host in WS_ALLOWED_ORIGINS

    async def open(self, *args: str, **kwargs: str) -> None:
        # Cache the connection
        self.uid = str(uuid.uuid4())
        self.wb_connection_manager.user_connections.add_connection(self)

        self.logger.debug(
            "WS open uid=%s remote_ip=%s origin=%s",
            self.uid,
            self.request.remote_ip,
            self.request.headers.get("Origin", "<none>"),
        )

        # Validate API key from query param or header at connection time
        status = await self.has_valid_api_key()
        if status is True:
            self._api_key_valid = True
        elif status == "API key is missing":
            self._api_key_valid = None
        else:
            self._api_key_valid = False
            self.logger.debug("WS closing uid=%s reason=api_key_failed status=%s", self.uid, status)
            self.close(4401, f"API key validation failed: {status}")

    async def on_message(self, message: str | bytes) -> None:
        self.logger.debug(f"Received message ({self.encoder.name}): {message!r}")
        try:
            message_data = self.encoder.decode(message)
        except Exception:
            msg_str = "It was impossible to parse the given data"
            self.logger.exception(f"Error: {msg_str}\nData: {message!r}")
            self.error_message(msg_str, code=1000)
            return

        try:
            # Assign message id
            msg_id = message_data.get("msg_id", None)
            service = message_data.get("service", None)

            if not msg_id or not service:
                raise HTTPException(
                    f"Missing input data; Data received: {json_encode(message_data, pretty=True)}",
                    code=1001,
                    http_status=400,
                )

            self.msg_id = int(msg_id)
            self.service = service
            res = self.find_service(service_name=service)
            if not res.result:
                raise HTTPException(
                    f"Could not find service by provided name: {self.service}",
                    code=1002,
                    http_status=400,
                )

            bridge_request, requires_api_key = res.result
            if not bridge_request:
                raise HTTPException(f"Service not found: {self.service}", code=1003, http_status=400)

            # Check cached API key validation from connection handshake
            if requires_api_key is not False:
                if self._api_key_valid is not True:
                    raise HTTPException(
                        "Application is not authenticated: API key is missing or was not provided at connection time",
                        code=1401,
                        http_status=401,
                    )

            # Set auth token
            auth_token = message_data.get("auth_token", None)
            if auth_token:
                await self.reload_token(auth_token)

            # Device session token auth
            device_session_token = message_data.get("device_session_token", None)
            if device_session_token and not self.device_id:
                await self._authenticate_device(device_session_token)

            # Timer
            async with self.timer.aenter("bridge.websockets.on_message.runRequestHandler"):
                req_data = message_data.get("data", {})
                action = req_data.get("action", None)
                input_data = req_data.get("data", {})

                if not action:
                    raise HTTPException("Missing action", code=1005, http_status=400)

                # Run request handler
                return_data = await bridge_request(action, input_data, self)
                if return_data is not None:
                    self.write_custom_message(return_data)

        except HTTPException as e:
            self.log_request(error=e)
            self.error_message(str(e.message), e.code or -1)
            return

        except Exception as e:
            self.log_request(error=e)
            self.logger.exception(f"Error processing request with exception: {e}")
            self.error_message(
                "Found an error while processing your request. Please try again later."
                " Send us a message if the problem persists."
            )
            return

        finally:
            self.msg_id = None
            self.service = None

            self.timer.print_timer_stats()
            self.timer.reset_timers()

    async def _authenticate_device(self, session_token: str) -> None:
        """Validate a device session token and register the device connection.
        Override in subclasses to implement project-specific device authentication."""
        pass

    def on_close(self):
        self.logger.debug(
            "WS close uid=%s code=%s reason=%s",
            self.uid,
            self.close_code,
            self.close_reason or "<none>",
        )
        if self.uid:
            self.wb_connection_manager.user_connections.remove_connection(self)
            if self.device_id:
                self.wb_connection_manager.device_connections.remove_connection(self)
                self.device_id = None
            self.uid = None
        self.auth_token = None
        self.current_user = None


# * WebSocketHandler - JSON WebSocket handler (backward compatible)
class WebSocketHandler(BaseWebSocketHandler):
    """JSON WebSocket handler at /v1/socket."""

    encoder = JsonEncoder()


# * MsgpackWebSocketHandler - MessagePack WebSocket handler
class MsgpackWebSocketHandler(BaseWebSocketHandler):
    """MessagePack WebSocket handler at /v1/socket/msgpack."""

    encoder = MsgpackEncoder()
