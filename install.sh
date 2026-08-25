#!/usr/bin/env bash
# Install the Soloist analog-out player onto this machine.
# Idempotent: safe to re-run after pulling changes.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$HOME/.config/soloist-hdmi/env"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"

echo "==> Installing packages"
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    pipewire pipewire-pulse pipewire-audio wireplumber pipewire-alsa \
    alsa-utils

echo "==> Installing PipeWire config"
mkdir -p "$HOME/.config/pipewire/pipewire.conf.d"
cp "$REPO/pipewire/10-hifi-aux.conf" "$HOME/.config/pipewire/pipewire.conf.d/"

echo "==> Installing WirePlumber config"
mkdir -p "$HOME/.config/wireplumber/wireplumber.conf.d"
cp "$REPO/wireplumber/50-aux-softmixer.conf" "$HOME/.config/wireplumber/wireplumber.conf.d/"

# The soft-mixer rule has to be in place before the hardware volume is pinned,
# or WirePlumber drives the control straight back to the top of its range.
echo "==> Restarting PipeWire"
systemctl --user daemon-reload
systemctl --user restart pipewire wireplumber pipewire-pulse
sleep 3

# The loopback needs the hardware sink's node name, which carries the SoC
# address and so differs per board. Discover it rather than committing it.
echo "==> Installing the mono/right loopback"
SINK_NODE="$("$REPO/scripts/find-audio-node.sh" --analog)"
echo "    analog sink: $SINK_NODE"
sed "s|__SINK_NODE__|$SINK_NODE|g" "$REPO/pipewire/20-mono-right.conf" \
    > "$HOME/.config/pipewire/pipewire.conf.d/20-mono-right.conf"
systemctl --user restart pipewire pipewire-pulse
sleep 3

# 0 dB, not 100%. On this card 100% is +4.00 dB of digital boost and clips.
echo "==> Pinning the analog output at 0 dB"
CARD="$(awk '/bcm2835 Headphones/ {print $1; exit}' /proc/asound/cards)"
amixer -c "${CARD:-0}" sset PCM 0dB >/dev/null
sudo alsactl store
# wpctl takes numeric object IDs, not node names, so resolve them first.
node_id() {
    pw-dump | python3 -c '
import json, sys
want = sys.argv[1]
for o in json.load(sys.stdin):
    p = (o.get("info") or {}).get("props") or {}
    if p.get("node.name") == want:
        print(o["id"]); break
' "$1"
}
for n in "$SINK_NODE" soloist_mono_right; do
    id="$(node_id "$n")"
    if [[ -n "$id" ]]; then
        wpctl set-volume "$id" 1.0
        wpctl set-mute   "$id" 0
        echo "    $n (id $id) -> volume 1.00, unmuted"
    else
        echo "    !! could not resolve node $n" >&2
    fi
done

echo "==> Installing systemd user units"
mkdir -p "$HOME/.config/systemd/user"
cp "$REPO/systemd/soloist.service" \
   "$REPO/systemd/soloist-update.service" \
   "$REPO/systemd/soloist-update.timer" \
   "$HOME/.config/systemd/user/"
systemctl --user daemon-reload

if [[ ! -f "$ENV_FILE" ]]; then
    echo "==> Creating $ENV_FILE from the example - EDIT IT AND ADD YOUR API KEY"
    mkdir -p "$(dirname "$ENV_FILE")"
    cp "$REPO/config/soloist.env.example" "$ENV_FILE"
fi
chmod 700 "$(dirname "$ENV_FILE")"
chmod 600 "$ENV_FILE"

echo "==> Enabling linger (so user services run without a login session)"
sudo loginctl enable-linger "$(id -un)"

# systemd would otherwise symlink ~/.local/state/soloist onto ~/.config/soloist.
echo "==> Ensuring a real state directory"
mkdir -p "$HOME/.local/state/soloist"

echo "==> Installing the current soloist build"
"$REPO/scripts/update-soloist.sh" || true

echo "==> Enabling services"
systemctl --user enable --now soloist-update.timer
systemctl --user enable soloist.service

if grep -q 'paste-your-key-here' "$ENV_FILE"; then
    echo
    echo "!! $ENV_FILE still has a placeholder API key."
    echo "!! Edit it, then run: systemctl --user start soloist.service"
    exit 0
fi

systemctl --user restart soloist.service
sleep 3
systemctl --user --no-pager status soloist.service | head -12
