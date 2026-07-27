import logging
from collections.abc import ItemsView

from py_app_runner.wbcm.ws_interface import WebSocketHandlerInterface


class DeviceConnections:
    """Tracks device WebSocket connections, mirroring UserConnections for devices."""

    # Connection is noted by connection uid
    device_connections: dict[str, WebSocketHandlerInterface]

    # Map device id to multiple connection uids
    device_id_map: dict[str, list[str]]

    logger: logging.Logger

    def __init__(self) -> None:
        self.device_connections = {}
        self.device_id_map = {}

        logger_name = f"{__name__}.{self.__class__.__name__}"
        self.logger = logging.getLogger(logger_name)

    def items(self) -> ItemsView[str, WebSocketHandlerInterface]:
        return self.device_connections.items()

    def add_connection(self, conn: WebSocketHandlerInterface) -> None:
        self.logger.debug("Adding device connection for conn_id %s", conn.uid)
        assert conn.uid, "Connection must have a uid"
        self.device_connections[conn.uid] = conn

    def remove_connection(self, conn: WebSocketHandlerInterface) -> None:
        self.logger.debug("Removing device connection for conn_id %s", conn.uid)
        assert conn.uid, "Connection must have a uid"
        if conn.uid in self.device_connections:
            del self.device_connections[conn.uid]

        self.unassign_connection(conn.uid)

    def unassign_connection(self, conn_id: str, keep_device_id: str | None = None) -> None:
        """Drop `conn_id` from every device's fan-out list except `keep_device_id`.

        No early exit: a re-authenticated socket may be present under several ids.
        """

        for device_id, conn_ids in list(self.device_id_map.items()):
            if device_id == keep_device_id:
                continue
            if conn_id in conn_ids:
                conn_ids.remove(conn_id)
                if not conn_ids:
                    del self.device_id_map[device_id]

    def reset(self) -> None:
        self.device_connections = {}
        self.device_id_map = {}

    def assign_device_id(self, conn_id: str, device_id: str) -> None:
        self.logger.debug("Assigning device_id %s to conn_id %s", device_id, conn_id)
        device_id = str(device_id)

        # A connection belongs to exactly one device at a time.
        self.unassign_connection(conn_id, keep_device_id=device_id)

        if device_id not in self.device_id_map:
            self.device_id_map[device_id] = []

        # Callers may re-assign on every re-auth; keep one entry per connection.
        if conn_id not in self.device_id_map[device_id]:
            self.device_id_map[device_id].append(conn_id)

    def remove_device_id(self, device_id: str) -> None:
        if device_id in self.device_id_map:
            del self.device_id_map[device_id]

    def get_connections_by_device_id(
        self,
        device_id: str,
    ) -> list[WebSocketHandlerInterface]:
        if device_id not in self.device_id_map:
            return []

        connections: list[WebSocketHandlerInterface] = []
        for conn_id in self.device_id_map[device_id]:
            if conn_id in self.device_connections:
                connections.append(self.device_connections[conn_id])

        return connections
