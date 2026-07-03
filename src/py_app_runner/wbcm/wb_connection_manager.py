import asyncio
import logging
import threading
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

from database_wrapper_redis import RedisDbAsync

from py_app_runner.registry import AppRegistry
from py_app_runner.tick_service import TickService
from py_app_runner.utils import json_decode
from py_app_runner.wbcm.device_connections import DeviceConnections
from py_app_runner.wbcm.factory import UserConnections


class WbConnectionManager:
    logger: logging.Logger
    tick_service: TickService

    cache_db: RedisDbAsync
    user_connections: UserConnections
    device_connections: DeviceConnections

    # Plain XREAD position (not a consumer group): every manager instance reads the
    # full stream, so forked workers each deliver to their own sockets. "$" = only
    # messages arriving after startup; missed history is useless to dead sockets.
    stream_last_id: str = "$"

    shutdown_lock: threading.Lock

    def __init__(self, cache_db: RedisDbAsync, debug: bool = False) -> None:
        assert cache_db is not None, "cache_db is required"

        loggerName = f"{__name__}.{self.__class__.__name__}"
        self.logger = logging.getLogger(loggerName)
        self.tick_service = TickService(1, self.tick, self.shutdown)

        self.cache_db = cache_db
        self.user_connections = UserConnections()
        self.device_connections = DeviceConnections()
        self.stream_last_id = "$"

        self.shutdown_lock = threading.Lock()

        debug_level = logging.DEBUG if debug else logging.INFO
        self.logger.setLevel(debug_level)
        self.tick_service.logger.setLevel(debug_level)
        self.tick_service.timer.logger.setLevel(debug_level)

    def __del__(self) -> None:
        self.logger.debug("Deleting WbConnectionManager")

    ####################
    ### Process loop ###
    ####################

    def start_in_new_loop(self) -> None:
        self.logger.debug("Starting WbConnectionManager in a new loop")
        self.tick_service.skip_signal_handling = True

        try:
            asyncio.run(self.start())
        except (KeyboardInterrupt, asyncio.CancelledError):
            ...  # Ignore this exception

    async def start(self):
        self.logger.info("Starting")
        await self.tick_service.run_loop()

    async def stop(self):
        self.logger.info("Stopping")
        await self.tick_service.stop_loop()

    async def shutdown(self):
        self.logger.debug("Stop the event loop")

        for _conn_id, conn in self.user_connections.items():
            try:
                conn.close()
            except Exception:
                pass

        for _conn_id, conn in self.device_connections.items():
            try:
                conn.close()
            except Exception:
                pass

        self.user_connections.reset()
        self.device_connections.reset()

    async def tick(self, tick_count: int, tick_time: float):
        try:
            async with self.connect_dbs():
                with self.tick_service.timer.enter("Get data"):
                    await self.process_messages()

        except Exception as e:
            self.logger.exception(f"Unknown error: {e}")

    #############
    ### Utils ###
    #############

    @asynccontextmanager
    async def connect_dbs(self) -> AsyncGenerator[bool]:
        try:
            self.logger.debug("Opening DB connections")
            await self.cache_db.open()

            yield True

        finally:
            self.logger.debug("Closing DB connections")
            await self.cache_db.close()

    async def read_stream_data(self, stream_name: str) -> Any:
        self.logger.debug(f"Reading stream data after {self.stream_last_id}")
        return await self.cache_db.connection.xread(
            streams={stream_name: self.stream_last_id},
            count=500,
            block=2000,
        )

    def send_message(
        self,
        user_id: str,
        message: bytes | str | dict[str, Any] | list[Any],
        source_uid: str,
    ) -> None:
        connections = self.user_connections.get_connections_by_user_id(user_id)
        if not connections:
            self.logger.debug(f"No connections for user {user_id}")
            return

        self.logger.debug(f"Sending message to {user_id}")
        for conn in connections:
            # Skip the source connection
            if conn.uid == source_uid:
                self.logger.debug(f"Skipping source connection {source_uid}")
                continue

            try:
                self.logger.debug(f"Sending message to {conn.uid} for user {user_id}")
                conn.loop.call_soon_threadsafe(
                    conn.write_custom_message,
                    message,
                    False,
                )
            except Exception as e:
                self.logger.warning(f"Removing connection {conn.uid} for exception {e}")
                self.user_connections.remove_connection(conn)
                return

    def send_message_to_device(
        self,
        device_id: str,
        message: bytes | str | dict[str, Any] | list[Any],
        source_uid: str,
    ) -> None:
        connections = self.device_connections.get_connections_by_device_id(device_id)
        if not connections:
            self.logger.debug("No connections for device %s", device_id)
            return

        self.logger.debug("Sending message to device %s", device_id)
        for conn in connections:
            if conn.uid == source_uid:
                continue
            try:
                conn.loop.call_soon_threadsafe(
                    conn.write_custom_message,
                    message,
                    False,
                )
            except Exception as e:
                self.logger.warning("Removing device connection %s for exception %s", conn.uid, e)
                self.device_connections.remove_connection(conn)
                return

    def close_sessions(
        self,
        user_id: str,
        source_uid: str,
        target_sid: str | None = None,
    ) -> None:
        """Hard-close revoked live sockets.

        Closing (not just notifying) is what makes a revoke enforceable: a client
        that ignores a notification must not keep an authenticated socket. With
        `target_sid` only that one session's socket is closed; otherwise all of
        the user's sockets except `source_uid`."""

        connections = self.user_connections.get_connections_by_user_id(user_id)
        for conn in connections:
            if conn.uid == source_uid:
                continue
            if target_sid is not None and conn.current_session_sid != target_sid:
                continue

            try:
                self.logger.debug(f"Closing connection {conn.uid} for user {user_id}")
                conn.loop.call_soon_threadsafe(conn.close)
            except Exception as e:
                self.logger.warning(f"Failed to close connection {conn.uid}: {e}")

    def send_message_to_all(
        self,
        message: bytes | str | dict[str, Any] | list[Any],
        source_uid: str,
    ) -> None:
        self.logger.debug("Sending message to all")
        for _user_id, conn in self.user_connections.items():
            # Skip the source connection
            if conn.uid == source_uid:
                self.logger.debug(f"Skipping source connection {source_uid}")
                continue

            try:
                self.logger.debug(f"Sending message to {conn.uid}")
                conn.loop.call_soon_threadsafe(
                    conn.write_custom_message,
                    message,
                    False,
                )
            except Exception as e:
                self.logger.warning(f"Removing connection {conn.uid} for exception {e}")
                self.user_connections.remove_connection(conn)
                continue

    ########################
    ### Process messages ###
    ########################

    async def process_messages(self) -> None:
        self.logger.debug("Find new messages in the stream")
        stream_name = AppRegistry.redis_channel()
        cached_data = await self.read_stream_data(stream_name)
        if not cached_data or stream_name not in cached_data or not cached_data[stream_name][0]:
            self.logger.debug("No records found")
            return

        items_found_count: int = 0
        message_data = cached_data[stream_name][0]

        self.logger.info(f"Processing {len(message_data)} records")
        for message_id, msg_data in message_data:
            if self.work(msg_data):
                items_found_count += 1

            self.stream_last_id = message_id

        self.logger.info(f"Processed {items_found_count} messages")

    def work(self, message_data: dict[str, Any]) -> bool:
        self.logger.debug(f"Processing message {message_data}")

        if "service" not in message_data:
            self.logger.error(f"Message data does not contain service: {message_data!r}")
            return False

        if "type" not in message_data:
            self.logger.error(f"Message data does not contain type: {message_data!r}")
            return False

        if "data" not in message_data:
            self.logger.error(f"Message data does not contain data: {message_data!r}")
            return False

        if "source_uid" not in message_data:
            self.logger.error(f"Message data does not contain source_uid: {message_data!r}")
            return False

        source_uid: str = message_data.get("source_uid", "")
        data = message_data.get("data", None)
        if data is not None and isinstance(data, str):
            message_data["data"] = json_decode(data)

        msg_type: str = message_data.get("type", "")

        # Session revocation closes sockets instead of forwarding a payload.
        if msg_type == "session_revoked":
            user_id = message_data.get("user_id", None)
            if user_id is None:
                self.logger.error(f"session_revoked without user_id: {message_data!r}")
                return False

            self.close_sessions(
                user_id,
                source_uid,
                message_data.get("target_sid", None) or None,
            )
            return True

        # Session relay: route to device or user based on target_type
        if msg_type == "session_relay":
            target_type = message_data.get("target_type", "")
            target_id = message_data.get("target_id", "")
            if target_type == "device":
                self.send_message_to_device(target_id, message_data, source_uid)
            elif target_type == "user":
                self.send_message(target_id, message_data, source_uid)
            else:
                self.logger.error("session_relay with unknown target_type: %s", target_type)
                return False
            return True

        # Default behavior: route by user_id
        if "user_id" not in message_data:
            self.logger.error(f"Message data does not contain user_id: {message_data!r}")
            return False

        user_id: str | None = message_data.get("user_id", None)
        if user_id is None:
            self.send_message_to_all(message_data, source_uid)
        else:
            self.send_message(user_id, message_data, source_uid)

        return True
