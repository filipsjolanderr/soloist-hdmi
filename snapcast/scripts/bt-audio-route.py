#!/usr/bin/env python3
"""Route any connected Bluetooth audio source to the aux speaker.

The Google Hub is paired to this box as its Bluetooth speaker, so it arrives as
an A2DP *source* - a `bluez_input.<MAC>.a2dp-source` node. PipeWire does not
link sources to sinks on its own (PulseAudio's module-bluetooth-policy used to;
WirePlumber ships no equivalent), so something has to make the connection.

Doing it by "capture from the default source" would be less code, but it would
also grab any future USB microphone and feed it straight to the speaker. This
matches bluez sources specifically, so nothing else gets routed by accident, and
it is MAC-agnostic - re-pairing, or a second phone, needs no config change.

One pw-loopback per source, because pw-loopback does a proper channel mix into
the MONO sink (0.5*FL + 0.5*FR) rather than summing both channels into one port
at full scale, which would clip.

Deliberately NOT routed through Snapcast: that path measured ~2 s of buffering,
which is fine for music but not for a Hub that also speaks timers and replies.
"""
import json
import os
import signal
import subprocess
import sys
import time

TARGET = os.environ.get("BT_ROUTE_TARGET", "aux_mono_right")
POLL_SECONDS = 2.0
PREFIX = "bluez_input."


def bluetooth_sources():
    """node.name of every connected Bluetooth audio source."""
    try:
        out = subprocess.run(["pw-dump"], capture_output=True, text=True,
                             timeout=10).stdout
        objects = json.loads(out)
    except (subprocess.SubprocessError, json.JSONDecodeError) as exc:
        print("pw-dump failed: %s" % exc, flush=True)
        return None            # None = "could not tell", distinct from "none found"

    found = set()
    for obj in objects:
        props = (obj.get("info") or {}).get("props") or {}
        name = props.get("node.name") or ""
        if props.get("media.class") == "Audio/Source" and name.startswith(PREFIX):
            found.add(name)
    return found


def main():
    routes = {}                # node.name -> Popen

    def shutdown(*_):
        for proc in routes.values():
            proc.terminate()
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    print("watching for Bluetooth sources -> %s" % TARGET, flush=True)
    while True:
        sources = bluetooth_sources()

        # Reap routes whose loopback died, so a crash is retried rather than
        # leaving the speaker silent until the next reconnect.
        for name in [n for n, p in routes.items() if p.poll() is not None]:
            print("route for %s exited (%s)" % (name, routes[name].returncode),
                  flush=True)
            del routes[name]

        if sources is not None:
            for name in sources - set(routes):
                print("connecting %s -> %s" % (name, TARGET), flush=True)
                routes[name] = subprocess.Popen(
                    ["pw-loopback", "--capture", name, "--playback", TARGET,
                     "--name", "bt-route"])

            for name in set(routes) - sources:
                print("disconnecting %s" % name, flush=True)
                routes[name].terminate()
                del routes[name]

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
