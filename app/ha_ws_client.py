"""Async wrapper around Home Assistant's WebSocket API.

Used for the device/entity registry — data Home Assistant's REST API does
not expose at all. Opens a short-lived connection per call rather than
holding one open, since these tools are used occasionally (audits, cleanup)
rather than on a hot path.
"""

from __future__ import annotations

import itertools
import json
import os
from typing import Any

import websockets


class HAWebSocketError(RuntimeError):
    """Raised when Home Assistant's WebSocket API returns an error."""


class HAWebSocketClient:
    def __init__(self, base_url: str | None = None, token: str | None = None) -> None:
        base_url = (base_url or os.environ["HA_URL"]).rstrip("/")
        self._token = token or os.environ["HA_TOKEN"]
        if base_url.startswith("https://"):
            ws_base = "wss://" + base_url[len("https://"):]
        elif base_url.startswith("http://"):
            ws_base = "ws://" + base_url[len("http://"):]
        else:
            ws_base = base_url
        self._url = f"{ws_base}/api/websocket"
        self._id_counter = itertools.count(1)

    async def _call(self, msg_type: str, **kwargs: Any) -> Any:
        async with websockets.connect(self._url, open_timeout=15, close_timeout=5) as ws:
            await ws.recv()  # auth_required
            await ws.send(json.dumps({"type": "auth", "access_token": self._token}))
            auth_resp = json.loads(await ws.recv())
            if auth_resp.get("type") != "auth_ok":
                raise HAWebSocketError(f"WebSocket auth failed: {auth_resp}")
            msg_id = next(self._id_counter)
            await ws.send(json.dumps({"id": msg_id, "type": msg_type, **kwargs}))
            while True:
                resp = json.loads(await ws.recv())
                if resp.get("id") != msg_id:
                    continue
                if not resp.get("success", False):
                    raise HAWebSocketError(f"{msg_type} -> {resp.get('error')}")
                return resp.get("result")

    async def list_devices(self) -> list[dict]:
        return await self._call("config/device_registry/list")

    async def get_device(self, device_id: str) -> dict:
        """Return the raw device registry entry (including `connections`,
        which carries the Zigbee IEEE address for ZHA devices) — unlike the
        server's list_devices tool, which trims the fields down."""
        devices = await self.list_devices()
        device = next((d for d in devices if d.get("id") == device_id), None)
        if device is None:
            raise HAWebSocketError(f"No device with id {device_id!r} found in the registry")
        return device

    async def list_registry_entities(self) -> list[dict]:
        return await self._call("config/entity_registry/list")

    async def remove_device(self, device_id: str) -> dict:
        """Remove a device from the registry by detaching it from every
        config entry it belongs to. This is how the HA UI's "Delete device"
        button works for devices that aren't tied to a live integration
        connection (e.g. a Zigbee device that's gone, or a leftover helper
        device) — for devices still actively provided by an integration, HA
        may recreate them on the next data refresh.
        """
        device = await self.get_device(device_id)
        config_entries = device.get("config_entries") or []
        if not config_entries:
            raise HAWebSocketError(
                f"Device {device_id!r} has no config_entries to detach from; "
                "cannot remove via this method"
            )
        last_result = None
        for entry_id in config_entries:
            last_result = await self._call(
                "config/device_registry/remove_config_entry",
                device_id=device_id,
                config_entry_id=entry_id,
            )
        return last_result or {"removed": True, "device_id": device_id}
