import logging
from collections.abc import ItemsView
from typing import NotRequired, TypedDict

from py_app_runner.wbcm.ws_interface import WebSocketHandlerInterface


class RedisMessage(TypedDict):
    type: str
    service: str
    user_id: str
    source_uid: str
    data: str | bytes | None
    # Only set for `session_revoked` messages targeting a single session.
    target_sid: NotRequired[str]


class UserConnections:
    # Connection is noted by machine id
    user_connections: dict[str, WebSocketHandlerInterface]

    # Map user id to multiple machine ids
    user_id_map: dict[str, list[str]]

    # Logger
    logger: logging.Logger

    def __init__(self) -> None:
        self.user_connections = {}
        self.user_id_map = {}

        logger_name = f"{__name__}.{self.__class__.__name__}"
        self.logger = logging.getLogger(logger_name)

    def items(self) -> ItemsView[str, WebSocketHandlerInterface]:
        """Return a flat list of all connections"""

        return self.user_connections.items()

    def add_connection(self, conn: WebSocketHandlerInterface) -> None:
        self.logger.debug(f"Adding connection for conn_id {conn.uid}")
        assert conn.uid, "Connection must have a uid"
        self.user_connections[conn.uid] = conn

        self.logger.debug(f"Currently we have {len(self.user_connections)} connections")

    def remove_connection(self, conn: WebSocketHandlerInterface) -> None:
        self.logger.debug(f"Removing connection for conn_id {conn.uid}")
        assert conn.uid, "Connection must have a uid"
        if conn.uid in self.user_connections:
            del self.user_connections[conn.uid]

        for user_id, conn_ids in list(self.user_id_map.items()):
            if conn.uid in conn_ids:
                conn_ids.remove(conn.uid)
                if not conn_ids:
                    del self.user_id_map[user_id]
                break

    def reset(self) -> None:
        self.logger.debug("Resetting user connections")
        self.user_connections = {}
        self.user_id_map = {}

    def assign_user_id(self, conn_id: str, user_id: str) -> None:
        self.logger.debug(f"Assigning userId {user_id} to conn_id {conn_id}")
        user_id = str(user_id)
        if user_id not in self.user_id_map:
            self.user_id_map[user_id] = []

        # Callers may re-assign on every re-auth/subscribe; keep one entry per connection.
        if conn_id not in self.user_id_map[user_id]:
            self.user_id_map[user_id].append(conn_id)

        self.logger.debug(
            f"Currently we have {len(self.user_id_map)} userIds assigned"
            f" to total of {len(self.user_connections)} connections"
        )

    def remove_user_id(self, user_id: str) -> None:
        self.logger.debug(f"Removing user_id {user_id}")
        if user_id in self.user_id_map:
            del self.user_id_map[user_id]

    def get_connections_by_user_id(
        self,
        user_id: str,
    ) -> list[WebSocketHandlerInterface]:
        self.logger.debug(f"Getting connections for user_id {user_id}")
        if user_id not in self.user_id_map:
            return []

        connections: list[WebSocketHandlerInterface] = []
        for conn_id in self.user_id_map[user_id]:
            if conn_id in self.user_connections:
                connections.append(self.user_connections[conn_id])

        return connections
