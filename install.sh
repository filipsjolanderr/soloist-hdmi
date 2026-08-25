#!/usr/bin/env bash
# Install one role of the Snapcast hi-fi onto this machine.
#
#   ./install.sh server    hifi2  - snapserver + Soloist + aux speaker + bluetooth
#   ./install.sh client    hifi   - snapclient + HDMI screen + receiver CEC
#
# Idempotent: safe to re-run after pulling changes.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$HOME/.config/soloist-hdmi/env"
SERVER_IP="${SNAPSERVER_HOST:-192.168.0.134}"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"

ROLE="${1:-}"
case "$ROLE" in
    server|client) ;;
    *) echo "usage: $(basename "$0") server|client" >&2; exit 2 ;;
esac

# wpctl takes numeric object IDs, not node names.
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

unity() {
    local id; id="$(node_id "$1")"
    if [[ -n "$id" ]]; then
        wpctl set-volume "$id" 1.0; wpctl set-mute "$id" 0
        echo "    $1 (id $id) -> 1.00, unmuted"
    fi
}

# --------------------------------------------------------------------------
if [[ "$ROLE" == server ]]; then
    echo "==> Installing packages"
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
        pipewire pipewire-pulse pipewire-audio wireplumber pipewire-alsa \
        alsa-utils snapserver snapclient python3-websockets bluez bluez-tools

    echo "==> PipeWire + WirePlumber config"
    mkdir -p "$HOME/.config/pipewire/pipewire.conf.d" \
             "$HOME/.config/wireplumber/wireplumber.conf.d"
    cp "$REPO/snapcast/server/10-hifi-aux.conf" "$HOME/.config/pipewire/pipewire.conf.d/"
    cp "$REPO/snapcast/server/30-snapcast-spotify.conf" "$HOME/.config/pipewire/pipewire.conf.d/"
    cp "$REPO/snapcast/server/50-aux-softmixer.conf" "$HOME/.config/wireplumber/wireplumber.conf.d/"

    echo "==> FIFO between PipeWire and snapserver"
    sudo cp "$REPO/snapcast/server/tmpfiles-snapcast.conf" /etc/tmpfiles.d/snapcast.conf
    sudo systemd-tmpfiles --create /etc/tmpfiles.d/snapcast.conf

    # soft-mixer must be in place before the hardware volume is pinned, or
    # WirePlumber drives the control straight back to the top of its range.
    systemctl --user daemon-reload
    systemctl --user restart pipewire wireplumber pipewire-pulse
    sleep 3

    echo "==> Mono/right loopback"
    SINK_NODE="$("$REPO/scripts/find-audio-node.sh" --analog)"
    echo "    analog sink: $SINK_NODE"
    sed "s|__SINK_NODE__|$SINK_NODE|g" "$REPO/snapcast/server/20-mono-right.conf" \
        > "$HOME/.config/pipewire/pipewire.conf.d/20-mono-right.conf"
    systemctl --user restart pipewire pipewire-pulse
    sleep 3

    # 0 dB, not 100%: on this card 100% is +4.00 dB of digital boost and clips.
    echo "==> Pinning the analog output at 0 dB"
    CARD="$(awk '/bcm2835 Headphones/ {print $1; exit}' /proc/asound/cards)"
    amixer -c "${CARD:-0}" sset PCM 0dB >/dev/null
    sudo alsactl store
    unity "$SINK_NODE"
    unity aux_mono_right

    echo "==> snapserver"
    sudo install -m 755 "$REPO/snapcast/plugins/meta_soloist.py" \
        /usr/share/snapserver/plug-ins/meta_soloist.py
    sudo cp "$REPO/snapcast/server/snapserver.conf" /etc/snapserver.conf
    sudo systemctl enable --now snapserver
    sudo systemctl restart snapserver

    echo "==> Bluetooth (the Google Hub pairs to this as its speaker)"
    sudo rfkill unblock bluetooth || true
    sudo cp "$REPO/snapcast/systemd/bt-agent.service" /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable --now bluetooth bt-agent
    sudo bluetoothctl -- power on        >/dev/null || true
    sudo bluetoothctl -- discoverable on >/dev/null || true
    sudo bluetoothctl -- pairable on     >/dev/null || true
    sudo bluetoothctl -- system-alias "Hi-Fi System" >/dev/null || true

    echo "==> Soloist + user units"
    mkdir -p "$HOME/.config/systemd/user" "$HOME/.local/state/soloist"
    cp "$REPO/systemd/soloist.service" \
       "$REPO/systemd/soloist-update.service" \
       "$REPO/systemd/soloist-update.timer" \
       "$REPO/snapcast/systemd/snapclient-aux.service" \
       "$REPO/snapcast/systemd/bt-audio-route.service" \
       "$HOME/.config/systemd/user/"
    # the packaged system client runs as _snapclient and cannot reach PipeWire
    sudo systemctl disable --now snapclient.service 2>/dev/null || true
    systemctl --user daemon-reload

    if [[ ! -f "$ENV_FILE" ]]; then
        mkdir -p "$(dirname "$ENV_FILE")"
        cp "$REPO/config/soloist.env.example" "$ENV_FILE"
        echo "!! created $ENV_FILE - add your API key"
    fi
    chmod 700 "$(dirname "$ENV_FILE")"; chmod 600 "$ENV_FILE"
    sudo loginctl enable-linger "$(id -un)"
    "$REPO/scripts/update-soloist.sh" || true

    systemctl --user enable --now soloist-update.timer snapclient-aux bt-audio-route
    systemctl --user enable soloist.service
    grep -q 'paste-your-key-here' "$ENV_FILE" \
        || systemctl --user restart soloist.service

# --------------------------------------------------------------------------
else
    echo "==> Installing packages"
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
        snapclient python3-pil python3-numpy python3-websockets v4l-utils \
        fonts-nunito-sans fonts-montserrat fonts-dejavu-core iw

    # brcmfmac defaults power save ON, which parks the radio between beacons and
    # turns a 2 ms LAN round trip into an occasional 70 ms one. Snapcast's time
    # sync times out on those spikes and the client tears down the whole session
    # and reconnects, roughly twice a minute, and every reconnect stops and
    # restarts the player - a few seconds of silence each time.
    echo "==> Disabling wifi power save"
    sudo cp "$REPO/snapcast/client-hdmi/wifi-powersave.conf" \
        /etc/NetworkManager/conf.d/wifi-powersave.conf
    sudo nmcli connection reload || true
    # immediate effect without bouncing the link
    sudo /usr/sbin/iw dev wlan0 set power_save off 2>/dev/null || true

    echo "==> CEC re-arm (a system unit: it writes sysfs and is udev-triggered)"
    sed "s|__REPO__|$REPO|g" "$REPO/snapcast/systemd/cec-rearm.service" \
        | sudo tee /etc/systemd/system/cec-rearm.service >/dev/null
    sudo cp "$REPO/snapcast/client-hdmi/99-cec-rearm.rules" /etc/udev/rules.d/
    sudo udevadm control --reload-rules
    sudo systemctl daemon-reload && sudo systemctl enable --now cec-rearm.service

    echo "==> User units"
    mkdir -p "$HOME/.config/systemd/user"
    for u in snapclient-hdmi snapcast-display snapcast-cec; do
        sed "s|__SERVER__|$SERVER_IP|g" "$REPO/snapcast/systemd/$u.service" \
            > "$HOME/.config/systemd/user/$u.service"
    done
    sudo systemctl disable --now snapclient.service 2>/dev/null || true
    sudo loginctl enable-linger "$(id -un)"

    if [[ ! -f "$ENV_FILE" ]]; then
        mkdir -p "$(dirname "$ENV_FILE")"
        cp "$REPO/config/client.env.example" "$ENV_FILE"
    fi
    chmod 700 "$(dirname "$ENV_FILE")"; chmod 600 "$ENV_FILE"

    # /dev/fb0 access comes from the video group.
    id -nG "$(id -un)" | tr " " "\n" | grep -qx video \
        || { sudo usermod -aG video "$(id -un)"; echo "    added to video group - log out and back in"; }

    systemctl --user daemon-reload
    systemctl --user enable --now snapclient-hdmi snapcast-display snapcast-cec
fi

echo "==> Done"
sleep 2
systemctl --user --no-pager status snapclient-*.service 2>/dev/null | grep -E 'Active:|●' | head -4
