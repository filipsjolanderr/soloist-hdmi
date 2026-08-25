#!/usr/bin/env bash
# Print PipeWire audio sinks, for SOLOIST_PIPEWIRE_NODE.
#
#   find-audio-node.sh            list every sink with its description
#   find-audio-node.sh --analog   print just the bcm2835 analog sink's node name
#
# --analog is what install.sh uses to substitute __SINK_NODE__; the card is
# matched by its ALSA name so this does not depend on the SoC address, which
# differs between Pi models.
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
        sinks.append((p.get("node.name"), p.get("node.description"), p.get("alsa.card_name"), o["id"]))

if mode == "--analog":
    for name, _desc, card, _id in sinks:
        if card == "bcm2835 Headphones":
            print(name)
            sys.exit(0)
    print("no bcm2835 analog sink found - is dtparam=audio=on set?", file=sys.stderr)
    sys.exit(1)

for name, desc, card, oid in sinks:
    print(name)
    print("    %s  (id %s)%s" % (desc, oid, "  <- analog jack" if card == "bcm2835 Headphones" else ""))
' "$mode"
