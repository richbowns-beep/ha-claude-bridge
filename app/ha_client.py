"""Thin async wrapper around the Home Assistant REST API.

Docs: https://developers.home-assistant.io/docs/api/rest/
"""

from __future__ import annotations

import os
from typing import Any

import httpx


class HAError(RuntimeError):
    """Raised when Home Assistant returns an error response."""


class HAClient:
    def __init__(self, base_url: str | None = None, token: str | None = None) -> None:
        base_url = (base_url or os.environ["HA_URL"]).rstrip("/")
        token = token or os.environ["HA_TOKEN"]
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            timeout=15.0,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        resp = await self._client.request(method, path, **kwargs)
        if resp.status_code >= 400:
            raise HAError(f"{method} {path} -> {resp.status_code}: {resp.text[:500]}")
        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError:
            return resp.text

    # ---- General -----------------------------------------------------

    async def config(self) -> Any:
        """Basic HA config/version info. Good connectivity check."""
        return await self._request("GET", "/api/config")

    async def get_states(self, domain: str | None = None) -> list[dict]:
        states: list[dict] = await self._request("GET", "/api/states")
        if domain:
            states = [s for s in states if s["entity_id"].startswith(f"{domain}.")]
        return states

    async def get_state(self, entity_id: str) -> dict:
        return await self._request("GET", f"/api/states/{entity_id}")

    async def call_service(
        self,
        domain: str,
        service: str,
        entity_id: str | None = None,
        data: dict | None = None,
    ) -> Any:
        payload = dict(data or {})
        if entity_id:
            payload["entity_id"] = entity_id
        return await self._request("POST", f"/api/services/{domain}/{service}", json=payload)

    # ---- Automations ----------------------------------------------------
    # Config CRUD: https://developers.home-assistant.io/docs/api/rest/#config-api

    async def get_automation(self, automation_id: str) -> dict:
        return await self._request("GET", f"/api/config/automation/config/{automation_id}")

    async def set_automation(self, automation_id: str, config: dict) -> Any:
        """Create or fully replace an automation. `config` should NOT include
        the `id` field; it's taken from `automation_id`."""
        return await self._request(
            "POST", f"/api/config/automation/config/{automation_id}", json=config
        )

    async def delete_automation(self, automation_id: str) -> Any:
        return await self._request("DELETE", f"/api/config/automation/config/{automation_id}")

    async def reload_automations(self) -> Any:
        return await self.call_service("automation", "reload")

    # ---- Scenes -----------------------------------------------------------

    async def get_scene(self, scene_id: str) -> dict:
        return await self._request("GET", f"/api/config/scene/config/{scene_id}")

    async def set_scene(self, scene_id: str, config: dict) -> Any:
        return await self._request("POST", f"/api/config/scene/config/{scene_id}", json=config)

    async def delete_scene(self, scene_id: str) -> Any:
        return await self._request("DELETE", f"/api/config/scene/config/{scene_id}")

    async def reload_scenes(self) -> Any:
        return await self.call_service("scene", "reload")
