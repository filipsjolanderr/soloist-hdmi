#!/usr/bin/env bash
# Print PipeWire audio sinks, for SOLOIST_PIPEWIRE_NODE and the loopback target.
#
#   find-audio-node.sh            list every sink with its description
#   find-audio-node.sh --analog   print just the node name of the speaker output
#
# --analog is what install.sh uses to substitute __SINK_NODE__. It prefers a USB
# DAC over the Pi's built-in 3.5mm jack: the built-in output is a PWM stage, so
# if something better is plugged in that is where the speaker is. It falls back
# to the built-in jack, so this still works with the USB adapter unplugged.
#
# Matching on the card rather than the node name keeps this free of the SoC
# address and of the DAC's USB serial number, both of which differ per box.
set -euo pipefail
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"

mode="${1:-list}"

pw-dump | python3 -c '
import json, sys

mode = sys.argv[1]
sinks = []
for o in json.load(sys.stdin):
    p = (o.get("info") or {}).get("props") or {}
    if p.get("media.class") == "Audio/Sink":
        sinks.append((p.get("node.name") or "", p.get("node.description"),
                      p.get("alsa.card_name"), o["id"]))

def is_usb(name):
    return "usb" in name.lower()

def is_builtin(card):
    return card == "bcm2835 Headphones"

if mode == "--analog":
    for name, _d, _c, _i in sinks:
        if is_usb(name):
            print(name); sys.exit(0)
    for name, _d, card, _i in sinks:
        if is_builtin(card):
            print(name); sys.exit(0)
    print("no analog sink found - no USB DAC, and is dtparam=audio=on set?",
          file=sys.stderr)
    sys.exit(1)

for name, desc, card, oid in sinks:
    if is_usb(name):
        tag = "  <- USB DAC (what --analog picks)"
    elif is_builtin(card):
        tag = "  <- built-in jack"
    else:
        tag = ""
    print(name)
    print("    %s  (id %s)%s" % (desc, oid, tag))
' "$mode"
