#!/usr/bin/env bash
# Give the HDMI-CEC adapter its physical address back.
#
# video=HDMI-A-1:1280x720@60D on the kernel command line puts the connector in
# DRM_FORCE_ON_DIGITAL. That is what keeps the sink - and so the Connect device
# - alive when the receiver is switched off, and it must stay.
#
# The cost is CEC. For a forced connector DRM skips the driver's ->detect()
# callback entirely, and vc4_hdmi is where the CEC physical address is set from
# the EDID. The EDID itself still arrives, because that happens in ->get_modes(),
# which does still run - so video, audio and the ELD all look perfectly healthy
# while the CEC adapter sits at f.f.f.f, never claims a logical address, and is
# invisible on the bus. Nothing in `cec-ctl` output hints at the connector being
# the reason.
#
# The adapter cannot simply be told its address: vc4_hdmi does not advertise
# CEC_CAP_PHYS_ADDR, so --phys-addr is refused. The address has to come from a
# real detect. So drop the force for exactly as long as one detect takes, then
# put it straight back - the address survives the re-force.
#
# Idempotent: does nothing if the adapter already has an address. Needs root.
set -euo pipefail

CONNECTOR="${CEC_CONNECTOR:-card0-HDMI-A-1}"
CEC_DEV="${CEC_DEV:-/dev/cec0}"
STATUS="/sys/class/drm/${CONNECTOR}/status"
# CEC caps OSD names at 14 characters; "Hi-Fi System" is 12, and matching the
# Connect name means the amplifier's input list and Spotify's device picker
# agree about what this box is called.
OSD_NAME="${CEC_OSD_NAME:-Hi-Fi System}"
TIMEOUT=10

# Serialise: the status writes below themselves generate udev change events,
# which is what triggers this script in the first place.
exec 9>/run/lock/cec-rearm.lock
flock 9

log() { echo "cec-rearm: $*"; }

# Is there actually a sink on the other end?
#
# This has to be answered without asking DRM, because DRM is forced and will
# say "connected" no matter what. Read the EDID straight off the DDC bus
# instead: i2c@7e805000 is the Pi's dedicated HDMI DDC bus, and a display
# answering at 0x50 with the EDID header means a real detect will succeed.
#
# The guard matters. Clearing the force while the receiver is off lets the
# detect come back "disconnected", and that is the cascade the README warns
# about - the vc4hdmi card loses its sink, PipeWire tears it down, and the
# Connect device does not return without a reboot. Not worth risking for an
# address that would not be there anyway.
sink_present() {
    modprobe i2c-dev 2>/dev/null || true
    local bus
    for bus in /dev/i2c-*; do
        [[ -e "$bus" ]] || continue
        if python3 - "$bus" <<'PY'
import fcntl, sys
try:
    f = open(sys.argv[1], "r+b", buffering=0)
    fcntl.ioctl(f, 0x0703, 0x50)      # I2C_SLAVE
    f.write(b"\x00")
    sys.exit(0 if f.read(8) == b"\x00\xff\xff\xff\xff\xff\xff\x00" else 1)
except Exception:
    sys.exit(1)
PY
        then
            return 0
        fi
    done
    return 1
}

phys_addr() {
    cec-ctl -d "$CEC_DEV" 2>/dev/null |
        awk -F': *' '/Physical Address/ { gsub(/ /, "", $2); print $2; exit }'
}

# How many logical addresses the adapter has been *asked* to claim, and the
# mask of the ones it actually holds. The two differ while the physical
# address is invalid: CEC_ADAP_S_LOG_ADDRS stores the request and the claim
# happens later, when an address turns up. That is the behaviour the early
# call below relies on.
la_count() {
    cec-ctl -d "$CEC_DEV" 2>/dev/null |
        awk -F': *' '/Logical Addresses +:/ { print $2+0; exit }'
}

la_mask() {
    cec-ctl -d "$CEC_DEV" 2>/dev/null |
        awk -F': *' '/Logical Address Mask/ { print $2; exit }'
}

# Give the adapter a *logical* address, which is a wholly separate thing from
# the physical one this script is otherwise about, and which nothing else on
# this box sets.
#
# cec-ctl only calls CEC_ADAP_S_LOG_ADDRS when it is given a device type
# (--playback, --tv, --audio, ...). Every invocation here and in the daemon
# omits one, so the adapter sat at zero logical addresses: holding a physical
# address, visible to DRM, and yet unable to transmit anything except
# <Image View On> from the Unregistered address, because that is the only
# message the CEC core lets an unconfigured adapter send. Every --to 5 query
# and standby failed with ENONET, silently.
#
# --playback claims logical address 4, falling back to 8 then 11. It will
# never take 5, so it cannot collide with the amplifier.
ensure_logical_address() {
    local n
    n="$(la_count || true)"
    if [[ "${n:-0}" -gt 0 ]]; then
        log "logical address already configured (mask $(la_mask || true))"
        return 0
    fi
    log "no logical address - configuring as a playback device"
    if cec-ctl -d "$CEC_DEV" --playback -o "$OSD_NAME" -s >/dev/null 2>&1; then
        log "configured, mask $(la_mask || true)"
    else
        log "could not configure logical addresses" >&2
    fi
}

[[ -e "$STATUS" ]] || { log "no such connector: $CONNECTOR" >&2; exit 1; }
[[ -e "$CEC_DEV" ]] || { log "no CEC device: $CEC_DEV" >&2; exit 1; }

# Before the early exits, not after: the hotplug path normally finds a valid
# physical address and returns just below, and the boot path can arrive here
# with the receiver off. Both still need to end up reachable on the bus.
ensure_logical_address

addr="$(phys_addr)"
if [[ -n "$addr" && "$addr" != "f.f.f.f" ]]; then
    log "already has an address ($addr), nothing to do"
    exit 0
fi

# DRM_FORCE_ON_DIGITAL can only be set back through debugfs - the sysfs status
# file understands "on", but that is plain DRM_FORCE_ON, which drops the "force
# HDMI rather than DVI signalling" half and takes the audio with it when the
# receiver is off. Find the debugfs knob before touching anything.
force_file=""
for f in /sys/kernel/debug/dri/*/"${CONNECTOR#card0-}"/force; do
    [[ -e "$f" ]] && { force_file="$f"; break; }
done
if [[ -z "$force_file" ]]; then
    log "debugfs force knob not found - is debugfs mounted?" >&2
    log "refusing to clear the force without a way to restore it" >&2
    exit 1
fi

original="$(cat "$force_file")"
log "connector force is '$original', CEC is unaddressed"

if ! sink_present; then
    log "no EDID on the DDC bus - receiver is off, leaving the force alone"
    exit 75
fi

if [[ "$original" == "unspecified" ]]; then
    log "connector is not forced, so CEC should address itself - is the receiver on?"
    exit 75
fi

restore() {
    echo "$original" > "$force_file"
    log "restored force '$original' (status now $(cat "$STATUS"))"
}
trap restore EXIT

# Clearing the force lets a real ->detect() run, which reads the EDID and hands
# the source physical address to the CEC adapter.
log "clearing force to allow one real detect"
echo detect > "$STATUS"

for (( i = 0; i < TIMEOUT * 2; i++ )); do
    addr="$(phys_addr)"
    [[ -n "$addr" && "$addr" != "f.f.f.f" ]] && break
    sleep 0.5
done

trap - EXIT
restore

addr="$(phys_addr)"
if [[ -z "$addr" || "$addr" == "f.f.f.f" ]]; then
    log "still unaddressed - the receiver is most likely off or on another input"
    exit 75  # EX_TEMPFAIL: not a fault, just nothing on the other end yet
fi

# The claim above may have been stored rather than acted on, if it ran while
# the adapter had no physical address to claim against. It has one now.
ensure_logical_address

logical="$(cec-ctl -d "$CEC_DEV" 2>/dev/null |
           awk -F': *' '/Logical Address +:/ { print $2; exit }')"
log "physical address $addr, logical address ${logical:-unknown}"
