# ha-claude-bridge

A small MCP server that lets Claude read and control your Home Assistant
instance — including creating and editing **schedules, scenes and
automations**, not just toggling devices. Built to run as a Docker container
next to Home Assistant on Unraid.

Why this exists instead of Home Assistant's built-in MCP/Assist support:
that integration is designed for controlling devices you've already exposed
to voice assistants, not for authoring new automations or scenes. This
bridge talks to Home Assistant's config API directly, so Claude can actually
write automation/scene definitions on your behalf.

## What it gives Claude

- `ha_status` — connectivity/version check
- `list_entities`, `get_state` — look things up
- `call_service` — general immediate control (turn on/off, set brightness,
  activate a scene, anything `domain.service`)
- `schedule_entity` — the common case: "turn X on at 07:00 and off at
  23:00[, on weekdays]". Re-running it for the same entity replaces the old
  schedule rather than duplicating it.
- `get_automation` / `set_automation` / `delete_automation` — full
  automation authoring for anything schedule_entity can't express
- `get_scene` / `set_scene` / `delete_scene` — scene authoring

Every write tool applies immediately to your real devices — there's no
staging/approval step inside the bridge itself. Claude has been instructed
to describe what it's about to do and check with you first, but you're
relying on that instruction, not a technical safeguard. Don't hand the
`BRIDGE_TOKEN` to anyone/anything you wouldn't trust with direct control of
your house.

## 1. Set up Home Assistant

1. Create a long-lived access token: your HA profile (bottom-left) →
   **Security** tab → **Long-lived access tokens** → **Create Token**. This
   token has whatever permissions your HA user account has — consider
   creating a dedicated non-admin HA user for this if you want to limit
   blast radius, though admin is required for the config API to work fully.
2. Note the address the bridge will use to reach HA. If HA runs with host
   networking on your Unraid box (the common default), this is just the
   box's LAN IP, e.g. `http://192.168.1.50:8123`.

## 2. Configure the bridge

```
cp .env.example .env
```

Fill in `HA_URL`, `HA_TOKEN`, and generate a `BRIDGE_TOKEN`:

```
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

`BRIDGE_TOKEN` is a second, separate secret — it's what protects *this*
bridge from anyone who finds its URL; `HA_TOKEN` is what the bridge uses to
talk to Home Assistant. Keep `.env` out of git (already in `.gitignore`).

## 3. Run it on Unraid

```
docker compose up -d --build
```

This starts the bridge on port 8000. Check the logs, then confirm it's
reachable:

```
curl -H "Authorization: Bearer <your BRIDGE_TOKEN>" http://<unraid-ip>:8000/mcp \
  -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"test","version":"0"}}}'
```

A 401 without the header and a real JSON-RPC response with it means it's
working.

## 4. Expose it so Claude's cloud servers can reach it

Claude connects over the public internet, so `http://<unraid-ip>:8000`
alone isn't reachable from outside your network. Your Nabu Casa and UniFi
Teleport setups don't help here — Teleport is a personal VPN for *your*
devices to join the home network, not a way for a third party (Claude) to
reach in.

**Recommended: Cloudflare Tunnel.** No port forwarding, no exposed firewall
rules — `cloudflared` makes an outbound connection to Cloudflare and gives
you a stable HTTPS URL (needs a domain added to a free Cloudflare account).
Unraid has a `cloudflared` Community App template, or run it as a sibling
container:

```yaml
  cloudflared:
    image: cloudflare/cloudflared:latest
    command: tunnel run
    environment:
      - TUNNEL_TOKEN=<from the Cloudflare Zero Trust dashboard>
    restart: unless-stopped
```

Point the tunnel's public hostname (e.g. `ha-bridge.yourdomain.com`) at
`ha-claude-bridge:8000` inside the tunnel config.

**Alternative:** a reverse proxy (e.g. Nginx Proxy Manager, which you may
already run on Unraid) with a port-forward and a real TLS certificate. More
moving parts, and opens a port on your router — Cloudflare Tunnel is less
work and doesn't require that.

## 5. Add it to Claude as a custom connector

In Claude's connector settings (Customize → Connectors → Add), add a custom
connector with your public URL, e.g. `https://ha-bridge.yourdomain.com/mcp`.
Set Authentication to **None** and add a Request header `authorization` with
value `Bearer <your BRIDGE_TOKEN>` — the same pattern used for this repo's
sibling GitHub MCP connector.

## 6. Try it

Once connected, in a Claude chat with this connector enabled:

> Turn the hallway light on at 7am and off at 11pm every day.

> Create a scene called "movie night" that dims the living room lights to
> 20% and turns off the hallway light.

> What's the current state of the bedroom light?

## Notes / limitations

- `set_automation`/`set_scene` fully replace an automation or scene by id —
  there's no partial-patch tool, so Claude reads the current config first
  when editing something that already exists.
- Finding an existing automation/scene's `id`: it's the last segment of the
  URL when editing it in the HA UI (`/config/automation/edit/<id>`).
  Anything created via `schedule_entity` uses ids like
  `bridge_<entity_slug>_on`.
- This bridge doesn't do OAuth; treat `BRIDGE_TOKEN` and `HA_TOKEN` like
  passwords. Rotate them (regenerate + update `.env` + restart) if you ever
  suspect either leaked.
