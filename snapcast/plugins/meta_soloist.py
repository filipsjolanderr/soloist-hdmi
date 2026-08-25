#!/usr/bin/env python3
"""Snapcast stream plugin: publish Soloist's now-playing state to snapserver.

snapserver knows nothing about what a `pipe://` source is carrying - it gets raw
PCM and no metadata. That is fine for playing audio and useless for anything
that wants to *display* it, which is why the Zero's screen and its CEC daemon
used to read Soloist's WebSocket directly. With Soloist now on the server box
they cannot, so this bridges the two: Soloist's WebSocket in, snapserver's
plugin protocol out, and every Snapcast client can then ask the server what is
playing over the ordinary control API.

Protocol: line-delimited JSON-RPC 2.0 on stdin/stdout, which is how snapserver
talks to a `controlscript=`. We send Plugin.Stream.Ready once, then a
Plugin.Stream.Player.Properties notification whenever the state changes.

Control (play/pause/next) is deliberately NOT implemented. Soloist's WebSocket
command envelope is not documented and guessing at it risks sending the wrong
thing to a live session; metadata is one-way and safe, and it is all the screen
and the receiver logic actually need. Requests are answered with a proper
JSON-RPC "method not found" rather than silently accepted.
"""
import asyncio
import json
import os
import sys
import threading

WS_URL = os.environ.get("SOLOIST_WS", "ws://127.0.0.1:3678")

import websockets

_out_lock = threading.Lock()


def send(obj):
    """One JSON object per line on stdout - snapserver reads line-delimited."""
    with _out_lock:
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()


def log(severity, message):
    send({"jsonrpc": "2.0", "method": "Plugin.Stream.Log",
          "params": {"severity": severity, "message": message}})


# --------------------------------------------------------------------------
# Soloist -> snapcast translation
# --------------------------------------------------------------------------
# Soloist's status vocabulary is wider than MPRIS's. Anything that is not
# actively moving is reported as paused rather than stopped, so a client can
# tell "there is a track, it is not moving" from "there is nothing".
_STATUS = {
    "playing": "playing",
    "paused": "paused",
    "stopped": "stopped",
    "idle": "stopped",
}


def properties_from(playback, auth):
    """Build a snapcast properties object from Soloist's playback_state.

    The item shape is Soloist's own nesting, not MPRIS: the title lives in
    decorations.identity.name, artists are a list of creator entities each with
    their own decorations, and the album is the parent entity's identity. This
    mirrors what the Zero's display used to parse, so the two agree on what a
    track is called.
    """
    status = playback.get("status") or "stopped"
    item = playback.get("item") or {}
    dec = item.get("decorations") or {}

    title = ((dec.get("identity") or {}).get("name") or "")

    creators = dec.get("creators") or []
    artists = []
    for c in creators:
        name = (((c.get("entity") or {}).get("decorations") or {})
                .get("identity", {}).get("name", ""))
        if name:
            artists.append(name)

    parent = (dec.get("parent") or {}).get("entity") or {}
    album = (((parent.get("decorations") or {}).get("identity") or {})
             .get("name") or "")

    covers = (dec.get("visual_identity") or {}).get("cover") or []
    by_size = {c.get("size"): c.get("url") for c in covers}
    art = (by_size.get("large") or by_size.get("xlarge")
           or by_size.get("default") or by_size.get("small"))

    metadata = {}
    if title:
        metadata["title"] = title
    if artists:
        metadata["artist"] = artists
    if album:
        metadata["album"] = album
    if art:
        metadata["artUrl"] = art

    duration = item.get("duration") or dec.get("duration")
    if isinstance(duration, (int, float)) and duration > 0:
        # Soloist counts in milliseconds; snapcast/MPRIS want seconds.
        metadata["duration"] = duration / 1000.0

    props = {
        "playbackStatus": _STATUS.get(status, "paused" if title else "stopped"),
        "canGoNext": False,
        "canGoPrevious": False,
        "canPlay": False,
        "canPause": False,
        "canSeek": False,
        "canControl": False,      # see the module docstring
        "metadata": metadata,
    }

    position = playback.get("position")
    if isinstance(position, (int, float)):
        props["position"] = position / 1000.0

    volume = playback.get("volume")
    if isinstance(volume, (int, float)):
        props["volume"] = int(round(volume if volume <= 100 else volume / 655.35))

    # Not part of the schema, but harmless and useful in the journal.
    if auth.get("device_name"):
        props.setdefault("metadata", {})
    return props


def publish(props):
    send({"jsonrpc": "2.0", "method": "Plugin.Stream.Player.Properties",
          "params": props})


# --------------------------------------------------------------------------
# snapserver -> us
# --------------------------------------------------------------------------
def serve_stdin(state):
    """snapserver's requests. Runs on its own thread: stdin is blocking and the
    WebSocket half is asyncio, and mixing the two in one loop buys nothing."""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue

        rid = req.get("id")
        method = req.get("method", "")

        if method == "Plugin.Stream.Player.GetProperties":
            send({"jsonrpc": "2.0", "id": rid, "result": state["props"]})
        elif rid is not None:
            send({"jsonrpc": "2.0", "id": rid,
                  "error": {"code": -32601,
                            "message": "Method not found",
                            "data": "%s is not implemented: this plugin is "
                                    "metadata-only" % method}})


# --------------------------------------------------------------------------
async def pump(state):
    auth = {}
    backoff = 1
    while True:
        try:
            async with websockets.connect(WS_URL, ping_interval=20) as ws:
                log("Info", "connected to soloist at %s" % WS_URL)
                backoff = 1
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if msg.get("type") == "auth_state":
                        auth = msg
                        continue
                    if msg.get("type") != "playback_state":
                        continue
                    props = properties_from(msg, auth)
                    if props != state["props"]:
                        state["props"] = props
                        publish(props)
        except Exception as exc:                       # noqa: BLE001
            # Soloist restarting is routine (the weekly build refresh does it).
            # Report stopped so a screen does not sit on a stale track forever.
            if state["props"].get("playbackStatus") != "stopped":
                state["props"] = {"playbackStatus": "stopped", "metadata": {},
                                  "canControl": False}
                publish(state["props"])
            log("Warning", "soloist connection lost (%s), retry in %ds"
                % (exc, backoff))
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)


def main():
    state = {"props": {"playbackStatus": "stopped", "metadata": {},
                       "canControl": False}}
    threading.Thread(target=serve_stdin, args=(state,), daemon=True).start()
    send({"jsonrpc": "2.0", "method": "Plugin.Stream.Ready"})
    publish(state["props"])
    asyncio.run(pump(state))


if __name__ == "__main__":
    main()
