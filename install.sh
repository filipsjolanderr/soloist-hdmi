#!/usr/bin/env bash
# Install the Soloist HDMI player onto this machine.
# Idempotent: safe to re-run after pulling changes.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$HOME/.config/soloist-hdmi/env"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"

echo "==> Installing packages"
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    pipewire pipewire-pulse pipewire-audio wireplumber pipewire-alsa \
    python3-pil python3-numpy python3-websockets v4l-utils \
    fonts-nunito-sans fonts-montserrat fonts-dejavu-core

echo "==> Installing PipeWire config"
mkdir -p "$HOME/.config/pipewire/pipewire.conf.d"
cp "$REPO/pipewire/10-hifi-hdmi.conf" "$HOME/.config/pipewire/pipewire.conf.d/"

echo "==> Installing systemd user units"
mkdir -p "$HOME/.config/systemd/user"
cp "$REPO/systemd/soloist.service" \
   "$REPO/systemd/soloist-update.service" \
   "$REPO/systemd/soloist-update.timer" \
   "$REPO/systemd/soloist-display.service" \
   "$REPO/systemd/soloist-cec.service" \
   "$HOME/.config/systemd/user/"

echo "==> Installing the CEC re-arm service"
# A system unit, not a user one: it writes sysfs/debugfs and is triggered by
# udev. %h does not exist for system units, so bake the repo path in.
sed "s|__REPO__|$REPO|g" "$REPO/systemd/cec-rearm.service" \
    | sudo tee /etc/systemd/system/cec-rearm.service >/dev/null
sudo cp "$REPO/udev/99-cec-rearm.rules" /etc/udev/rules.d/
sudo udevadm control --reload-rules
sudo systemctl daemon-reload
sudo systemctl enable cec-rearm.service

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

echo "==> Reloading systemd and restarting PipeWire"
systemctl --user daemon-reload
systemctl --user restart pipewire wireplumber pipewire-pulse
sleep 2

echo "==> Enabling services"
systemctl --user enable --now soloist-update.timer
systemctl --user enable soloist.service
systemctl --user enable soloist-display.service
systemctl --user enable soloist-cec.service

# /dev/fb0 access comes from the video group.
if ! id -nG "$(id -un)" | tr " " "\n" | grep -qx video; then
    echo "==> Adding $(id -un) to the video group (log out and back in to apply)"
    sudo usermod -aG video "$(id -un)"
fi

if grep -q 'paste-your-key-here' "$ENV_FILE"; then
    echo
    echo "!! $ENV_FILE still has a placeholder API key."
    echo "!! Edit it, then run: systemctl --user start soloist.service"
    exit 0
fi

systemctl --user restart soloist.service
systemctl --user restart soloist-display.service
systemctl --user restart soloist-cec.service
sleep 3
systemctl --user --no-pager status soloist.service | head -12
