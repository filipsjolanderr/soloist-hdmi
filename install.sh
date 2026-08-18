#!/usr/bin/env bash
# Install the Soloist HDMI player onto this machine.
# Idempotent: safe to re-run after pulling changes.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$HOME/.config/soloist-hdmi/env"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"

echo "==> Installing PipeWire config"
mkdir -p "$HOME/.config/pipewire/pipewire.conf.d"
cp "$REPO/pipewire/10-hifi-hdmi.conf" "$HOME/.config/pipewire/pipewire.conf.d/"

echo "==> Installing systemd user units"
mkdir -p "$HOME/.config/systemd/user"
cp "$REPO/systemd/soloist.service" \
   "$REPO/systemd/soloist-update.service" \
   "$REPO/systemd/soloist-update.timer" \
   "$HOME/.config/systemd/user/"

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

if grep -q 'paste-your-key-here' "$ENV_FILE"; then
    echo
    echo "!! $ENV_FILE still has a placeholder API key."
    echo "!! Edit it, then run: systemctl --user start soloist.service"
    exit 0
fi

systemctl --user restart soloist.service
sleep 3
systemctl --user --no-pager status soloist.service | head -20
