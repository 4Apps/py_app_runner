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

        for _device_id, conn_ids in self.device_id_map.items():
            if conn.uid in conn_ids:
                conn_ids.remove(conn.uid)
                break

    def reset(self) -> None:
        self.device_connections = {}
        self.device_id_map = {}

    def assign_device_id(self, conn_id: str, device_id: str) -> None:
        self.logger.debug("Assigning device_id %s to conn_id %s", device_id, conn_id)
        device_id = str(device_id)
        if device_id not in self.device_id_map:
            self.device_id_map[device_id] = []

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
