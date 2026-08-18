#!/usr/bin/env bash
# Print the PipeWire node name of each audio sink, for SOLOIST_PIPEWIRE_NODE.
set -euo pipefail
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
pw-dump | python3 -c '
import json, sys
for o in json.load(sys.stdin):
    p = (o.get("info") or {}).get("props") or {}
    if p.get("media.class") == "Audio/Sink":
        name = p.get("node.name")
        desc = p.get("node.description")
        print(name)
        print("    " + str(desc) + "  (id " + str(o["id"]) + ")")
'
