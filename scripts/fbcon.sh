#!/usr/bin/env bash
# Release or restore the framebuffer console (fbcon).
#
# fbcon owns /dev/fb0 by default and repaints the text console over anything we
# draw. Unbinding it hands the framebuffer to the display service; rebinding
# brings the login console back on the TV.
#
# Needs root. Usage: fbcon.sh release|restore
set -euo pipefail

action="${1:-}"
case "$action" in
    release) want=0 ;;
    restore) want=1 ;;
    *) echo "usage: $(basename "$0") release|restore" >&2; exit 2 ;;
esac

found=0
for vtcon in /sys/class/vtconsole/vtcon*; do
    # Match by name rather than assuming vtcon1 - the numbering is not fixed.
    if grep -q "frame buffer device" "$vtcon/name" 2>/dev/null; then
        echo "$want" > "$vtcon/bind"
        echo "$(basename "$vtcon") ($(cat "$vtcon/name")) bind=$want"
        found=1
    fi
done

if [[ "$found" -eq 0 ]]; then
    echo "no framebuffer vtconsole found - nothing to do" >&2
fi
