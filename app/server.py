"""MCP server exposing Home Assistant automation/scene/schedule control to Claude.

Run directly for local testing:
    HA_URL=http://homeassistant.local:8123 HA_TOKEN=... BRIDGE_TOKEN=... \
        python3 -m app.server

In Docker, gunicorn/uvicorn serves `app` from this module (see Dockerfile).
"""

from __future__ import annotations

import os
import re
from typing import Any

from mcp.server.fastmcp import FastMCP

from .auth import protect
from .ha_client import HAClient
from .ha_ws_client import HAWebSocketClient

WEEKDAYS = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}

mcp = FastMCP(
    name="Home Assistant Bridge",
    instructions=(
        "Tools for controlling Rich's Home Assistant instance: querying and "
        "controlling devices, and creating/editing schedules, scenes and "
        "automations. Always confirm the entity_id and the resulting "
        "automation/scene config with the user in plain language before "
        "calling a write tool (set_automation, set_scene, delete_automation, "
        "delete_scene, call_service with a state-changing service), since "
        "these take effect immediately on real devices."
    ),
    host="0.0.0.0",
    port=int(os.environ.get("PORT", "8000")),
    stateless_http=True,
)

_client: HAClient | None = None
_ws_client: HAWebSocketClient | None = None


def get_client() -> HAClient:
    global _client
    if _client is None:
        _client = HAClient()
    return _client


def get_ws_client() -> HAWebSocketClient:
    global _ws_client
    if _ws_client is None:
        _ws_client = HAWebSocketClient()
    return _ws_client


def _slugify(entity_id: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", entity_id.lower()).strip("_")


# ---- Read-only / status tools -------------------------------------------


@mcp.tool()
async def ha_status() -> dict:
    """Check connectivity to Home Assistant and return its version/location
    info. Use this first if anything else is failing."""
    return await get_client().config()


@mcp.tool()
async def list_entities(domain: str | None = None, search: str | None = None) -> list[dict]:
    """List Home Assistant entities, optionally filtered.

    Args:
        domain: e.g. "light", "switch", "scene", "automation", "climate".
            Omit to list everything (can be large).
        search: case-insensitive substring match against entity_id or
            friendly name.
    """
    states = await get_client().get_states(domain=domain)
    out = []
    for s in states:
        friendly = s.get("attributes", {}).get("friendly_name", "")
        if search:
            needle = search.lower()
            if needle not in s["entity_id"].lower() and needle not in friendly.lower():
                continue
        out.append({"entity_id": s["entity_id"], "state": s["state"], "friendly_name": friendly})
    return out


@mcp.tool()
async def get_state(entity_id: str) -> dict:
    """Get the current state and attributes of one entity, e.g. 'light.bedroom'."""
    return await get_client().get_state(entity_id)


# ---- Direct device control ------------------------------------------------


@mcp.tool()
async def call_service(
    domain: str,
    service: str,
    entity_id: str | None = None,
    data: dict[str, Any] | None = None,
) -> Any:
    """Call any Home Assistant service — the general-purpose escape hatch for
    immediate device control (e.g. domain='light', service='turn_on',
    entity_id='light.bedroom', data={'brightness_pct': 50}), activating a
    scene (domain='scene', service='turn_on', entity_id='scene.movie_night'),
    etc. This acts immediately on the real device."""
    return await get_client().call_service(domain, service, entity_id=entity_id, data=data)


# ---- Scheduling convenience tool ------------------------------------------


@mcp.tool()
async def schedule_entity(
    entity_id: str,
    on_time: str | None = None,
    off_time: str | None = None,
    days: list[str] | None = None,
    name: str | None = None,
) -> dict:
    """Create or replace a simple daily on/off schedule for one entity, e.g.
    "turn the hallway light on at 07:00 and off at 23:00 on weekdays".

    Args:
        entity_id: e.g. "light.hallway".
        on_time: "HH:MM" (24h) or None to skip creating an on-automation.
        off_time: "HH:MM" (24h) or None to skip creating an off-automation.
        days: optional list from {mon,tue,wed,thu,fri,sat,sun}; omit for every day.
        name: friendly label used in the automation name; defaults to entity_id.

    Re-running this with the same entity_id replaces the existing schedule
    for that entity (idempotent) rather than creating duplicates.
    """
    if not on_time and not off_time:
        raise ValueError("Provide at least one of on_time or off_time")
    if days:
        bad = [d for d in days if d.lower() not in WEEKDAYS]
        if bad:
            raise ValueError(f"Unknown day(s) {bad}; use three-letter lowercase e.g. mon,tue")

    client = get_client()
    slug = _slugify(entity_id)
    label = name or entity_id
    created = {}

    def _condition() -> list[dict]:
        if not days:
            return []
        return [{"condition": "time", "weekday": [d.lower() for d in days]}]

    if on_time:
        aid = f"bridge_{slug}_on"
        config = {
            "alias": f"{label} - scheduled on",
            "mode": "single",
            "trigger": [{"platform": "time", "at": on_time}],
            "condition": _condition(),
            "action": [{"service": "homeassistant.turn_on", "target": {"entity_id": entity_id}}],
        }
        await client.set_automation(aid, config)
        created["on_automation_id"] = aid

    if off_time:
        aid = f"bridge_{slug}_off"
        config = {
            "alias": f"{label} - scheduled off",
            "mode": "single",
            "trigger": [{"platform": "time", "at": off_time}],
            "condition": _condition(),
            "action": [{"service": "homeassistant.turn_off", "target": {"entity_id": entity_id}}],
        }
        await client.set_automation(aid, config)
        created["off_automation_id"] = aid

    await client.reload_automations()
    return {"entity_id": entity_id, "on_time": on_time, "off_time": off_time, "days": days, **created}


# ---- Automations (full control) -------------------------------------------


@mcp.tool()
async def get_automation(automation_id: str) -> dict:
    """Get the raw config of one automation by its id (the id shown at the
    end of the URL when editing it in the HA UI, e.g.
    'https://.../config/automation/edit/1699999999999' -> id '1699999999999';
    automations created by schedule_entity use ids like 'bridge_<slug>_on')."""
    return await get_client().get_automation(automation_id)


@mcp.tool()
async def set_automation(
    automation_id: str,
    alias: str,
    trigger: list[dict],
    action: list[dict],
    condition: list[dict] | None = None,
    mode: str = "single",
) -> Any:
    """Create a new automation or fully replace an existing one by id. Use
    this for anything schedule_entity can't express (multiple entities,
    non-time triggers like state/sun/webhook, multi-step actions). trigger/
    condition/action use standard Home Assistant automation YAML structure
    translated to JSON, e.g. trigger=[{"platform": "state", "entity_id":
    "binary_sensor.front_door", "to": "on"}]. Applies immediately."""
    config = {
        "alias": alias,
        "mode": mode,
        "trigger": trigger,
        "condition": condition or [],
        "action": action,
    }
    result = await get_client().set_automation(automation_id, config)
    await get_client().reload_automations()
    return result


@mcp.tool()
async def delete_automation(automation_id: str) -> Any:
    """Delete an automation by id. Irreversible — confirm with the user first."""
    result = await get_client().delete_automation(automation_id)
    await get_client().reload_automations()
    return result


# ---- Scenes -----------------------------------------------------------------


@mcp.tool()
async def get_scene(scene_id: str) -> dict:
    """Get the raw config of one scene by id (see get_automation for how ids work)."""
    return await get_client().get_scene(scene_id)


@mcp.tool()
async def set_scene(scene_id: str, name: str, entities: dict[str, Any]) -> Any:
    """Create a new scene or fully replace an existing one by id. `entities`
    maps entity_id to the desired state, e.g. {"light.living_room": {"state":
    "on", "brightness": 180, "color_temp": 350}, "switch.lamp": "on"}.
    Activate a scene afterwards with call_service(domain='scene',
    service='turn_on', entity_id=scene_id)."""
    config = {"name": name, "entities": entities}
    result = await get_client().set_scene(scene_id, config)
    await get_client().reload_scenes()
    return result


@mcp.tool()
async def delete_scene(scene_id: str) -> Any:
    """Delete a scene by id. Irreversible — confirm with the user first."""
    result = await get_client().delete_scene(scene_id)
    await get_client().reload_scenes()
    return result


# ---- Device/entity registry (WebSocket API) --------------------------------
#
# Home Assistant's REST API has no visibility into the device/entity
# registry — the data behind Settings -> Devices & Services -> Devices, and
# what "device actions" in automations actually point at. These tools use
# HA's WebSocket API instead, which is the only way to read or manage it.


@mcp.tool()
async def list_devices(search: str | None = None) -> list[dict]:
    """List devices from Home Assistant's device registry (Settings ->
    Devices & Services -> Devices). This is different from list_entities:
    a device is a physical/logical thing (a Zigbee bulb, a hub) that owns
    one or more entities. Useful for finding dead/leftover devices, or the
    device_id behind an automation's "device action".

    Args:
        search: case-insensitive substring match against the device's name.
    """
    devices = await get_ws_client().list_devices()
    out = []
    for d in devices:
        name = d.get("name_by_user") or d.get("name") or ""
        if search and search.lower() not in name.lower():
            continue
        out.append(
            {
                "id": d.get("id"),
                "name": name,
                "manufacturer": d.get("manufacturer"),
                "model": d.get("model"),
                "area_id": d.get("area_id"),
                "config_entries": d.get("config_entries"),
                "disabled_by": d.get("disabled_by"),
            }
        )
    return out


@mcp.tool()
async def list_registry_entities(device_id: str | None = None, search: str | None = None) -> list[dict]:
    """List entries from Home Assistant's entity registry — includes
    metadata list_entities doesn't have (which device an entity belongs to,
    which integration/platform created it, whether it's disabled or hidden),
    including entities with no current state (e.g. belonging to an
    unavailable device).

    Args:
        device_id: filter to entities belonging to one device (from list_devices).
        search: case-insensitive substring match against entity_id or name.
    """
    entities = await get_ws_client().list_registry_entities()
    out = []
    for e in entities:
        if device_id and e.get("device_id") != device_id:
            continue
        name = e.get("name") or e.get("original_name") or ""
        eid = e.get("entity_id", "")
        if search:
            needle = search.lower()
            if needle not in eid.lower() and needle not in name.lower():
                continue
        out.append(
            {
                "entity_id": eid,
                "name": name,
                "device_id": e.get("device_id"),
                "platform": e.get("platform"),
                "disabled_by": e.get("disabled_by"),
                "hidden_by": e.get("hidden_by"),
            }
        )
    return out


@mcp.tool()
async def remove_device(device_id: str) -> dict:
    """Permanently remove a device from Home Assistant's device registry
    (and its entities with it) — the same effect as the "Delete" button on
    a device's page in the HA UI. Irreversible — confirm the device's name
    and id with the user in plain language first (use list_devices to find
    it). Devices still actively provided by a live integration connection
    may be recreated automatically; this is intended for dead/leftover
    devices (e.g. one that's been unpaired or removed)."""
    return await get_ws_client().remove_device(device_id)


app = protect(mcp.streamable_http_app())


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
