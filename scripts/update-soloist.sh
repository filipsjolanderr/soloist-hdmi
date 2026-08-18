#!/usr/bin/env bash
# Download and install the current Spotify Soloist build, then restart the service.
#
# Soloist builds expire 90 days after their build date and exit with code 10.
# On a headless box that means the player just stops working one day, so this
# runs on a timer to stay ahead of the expiry.
set -euo pipefail

BASE_URL="https://soloist-builds.spotifycdn.com"
DEST="/usr/local/bin/soloist"

case "$(uname -m)" in
    aarch64) ARCH="arm64" ;;
    armv7l)  ARCH="arm32" ;;
    x86_64)  ARCH="x86_64" ;;
    *) echo "unsupported architecture: $(uname -m)" >&2; exit 1 ;;
esac

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

echo "Fetching soloist_release_${ARCH}.tar.gz ..."
curl --fail --location --silent --show-error \
     -o "$tmp/soloist.tar.gz" \
     "${BASE_URL}/soloist_release_${ARCH}.tar.gz"

tar -xzf "$tmp/soloist.tar.gz" -C "$tmp"
test -x "$tmp/soloist"

new_ver="$("$tmp/soloist" --version)"
old_ver="$("$DEST" --version 2>/dev/null || echo "(not installed)")"

if [[ "$new_ver" == "$old_ver" ]]; then
    echo "Already current: $new_ver"
    exit 0
fi

echo "Updating:"
echo "  from: $old_ver"
echo "    to: $new_ver"

sudo install -m 755 "$tmp/soloist" "$DEST"

# RestartPreventExitStatus=10 leaves the unit failed once a build has expired,
# so reset-failed before restarting or the start is refused.
systemctl --user reset-failed soloist.service 2>/dev/null || true
systemctl --user restart soloist.service
echo "Updated and restarted."
