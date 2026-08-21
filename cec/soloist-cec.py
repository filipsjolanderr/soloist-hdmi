#!/usr/bin/env python3
"""Drive the receiver over HDMI-CEC from Soloist's playback state.

Two jobs, both of them things you would otherwise reach for the remote to do:

  * when a Connect session starts or playback begins, wake the receiver and
    make this Pi the active source, so picking "Hi-Fi System" in Spotify is
    the only action needed to get sound out of the speakers;
  * when nothing has played for a while, put the receiver back into standby.

Listens to the same WebSocket API the now-playing screen uses, and shells out
to cec-ctl rather than talking to /dev/cec0 directly - the traffic is a handful
of messages an hour, and v4l-utils is already a dependency.

CEC on this box only works because scripts/cec-rearm.sh borrows an unforced
detect to get a physical address; see the README. If the address has gone
(f.f.f.f) this re-arms before giving up, because the usual reason to have lost
it is the very receiver power cycle we are now trying to react to.
"""
import asyncio, json, logging, os, subprocess, sys, time
from pathlib import Path

import websockets

LOG = logging.getLogger("cec")

STATE_DIR = Path(os.environ.get("STATE_DIRECTORY",
                 Path.home() / ".local/state/soloist"))
REPO = Path(__file__).resolve().parent.parent

CEC_DEV = os.environ.get("SOLOIST_CEC_DEVICE", "/dev/cec0")
# 5 is the Audio System. Deliberately not broadcast: a broadcast <Standby>
# would take the TV down with the amplifier, and the TV is not ours to switch
# off - someone may well be watching it with the music paused.
AMP = os.environ.get("SOLOIST_CEC_AMP", "5")
# CEC caps OSD names at 14 characters. Matching the Connect name keeps the
# amplifier's input list and Spotify's device picker in agreement.
OSD_NAME = os.environ.get("SOLOIST_CEC_OSD_NAME", "Hi-Fi System")[:14]
IDLE_MINUTES = float(os.environ.get("SOLOIST_CEC_IDLE_MINUTES", "30"))
DO_STANDBY = os.environ.get("SOLOIST_CEC_STANDBY", "1") not in ("0", "no", "false")
DO_WAKE = os.environ.get("SOLOIST_CEC_WAKE", "1") not in ("0", "no", "false")

INVALID = "f.f.f.f"
IDLE_POLL_SECONDS = 30


# --------------------------------------------------------------------------
# cec-ctl
# --------------------------------------------------------------------------
def cec(*args, timeout=15):
    """Run cec-ctl and hand back its output, or None if it could not run."""
    try:
        p = subprocess.run(["cec-ctl", "-d", CEC_DEV, *args],
                           capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as e:
        LOG.warning("cec-ctl %s failed: %s", " ".join(args), e)
        return None
    if p.returncode != 0:
        LOG.warning("cec-ctl %s exited %d: %s", " ".join(args), p.returncode,
                    (p.stderr or "").strip() or "(no stderr)")
        return None
    return p.stdout


def tx_failed(out):
    """Did a transmit actually reach the bus?

    cec-ctl exits 0 whether the message went out or was never acknowledged,
    so the exit status says nothing; the Tx line it prints is the only honest
    signal. A successful query has no Tx line at all - the reply is the proof
    - while a failure reads "Tx, Not Acknowledged (4), Max Retries" and a
    query nobody answers reads "Tx, OK, Rx, Timeout".
    """
    if out is None:
        return True
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("Tx,"):
            return not line.startswith("Tx, OK")
    return False


def fields(out):
    """cec-ctl's "Name : value" lines, keyed by name.

    Substring matching is not good enough here: the Capabilities block lists
    bare feature names - "Logical Addresses", "Physical Address", "Transmit"
    - with no colon and no value, and those collide with the real fields.
    """
    found = {}
    for line in (out or "").splitlines():
        key, sep, val = line.partition(":")
        if sep and val.strip():
            found.setdefault(key.strip(), val.strip())
    return found


def phys_addr():
    return fields(cec()).get("Physical Address")


def logical_addrs():
    """(addresses we asked for, mask of the ones we hold).

    The two differ while the physical address is invalid: CEC_ADAP_S_LOG_ADDRS
    stores the request and the claim happens when an address turns up.
    """
    f = fields(cec())
    try:
        mask = int(f.get("Logical Address Mask", "0"), 16)
    except ValueError:
        mask = 0
    try:
        # "1 (Allow RC Passthrough)" - the count is the leading number.
        count = int(f.get("Logical Addresses", "0").split()[0])
    except (ValueError, IndexError):
        count = 0
    return count, mask


def configure():
    """Claim a logical address, without which we cannot talk to the amplifier.

    cec-ctl only calls CEC_ADAP_S_LOG_ADDRS when it is given a device type,
    and none of the calls here pass one - so left alone the adapter holds a
    physical address and no logical address at all. The CEC core lets such an
    adapter transmit exactly one thing, <Image View On> from the Unregistered
    address to the TV; every --to 5 message fails with ENONET. That is a
    silent failure, which is why this is checked rather than assumed.

    --playback takes logical address 4, falling back to 8 then 11. Never 5,
    so it cannot collide with the amplifier we are trying to talk to.
    """
    _, mask = logical_addrs()
    if mask:
        return True
    LOG.info("no logical address, configuring as a playback device")
    cec("--playback", "-o", OSD_NAME, "-s", timeout=30)
    _, mask = logical_addrs()
    if not mask:
        LOG.warning("could not claim a logical address - cannot reach the bus")
        return False
    LOG.info("claimed logical address mask 0x%04x as %r", mask, OSD_NAME)
    return True


def rearm():
    """Ask cec-rearm.sh for an address back. Needs root, hence sudo."""
    try:
        p = subprocess.run(["sudo", "-n", str(REPO / "scripts/cec-rearm.sh")],
                           capture_output=True, text=True, timeout=60)
        # 75 is the script's "receiver is off, nothing to take an address from"
        if p.returncode not in (0, 75):
            LOG.warning("cec-rearm failed (%d): %s",
                        p.returncode, p.stderr.strip())
        return p.returncode == 0
    except (OSError, subprocess.SubprocessError) as e:
        LOG.warning("could not run cec-rearm: %s", e)
        return False


def ready():
    """Our physical address, re-arming once if it has gone.

    Both addresses have to be in place: the physical one says which input we
    are behind, the logical one is what lets us transmit at all.
    """
    addr = phys_addr()
    if not addr or addr == INVALID:
        LOG.info("no CEC address (%s), re-arming", addr)
        rearm()
        addr = phys_addr()
        if not addr or addr == INVALID:
            # Expected when the receiver is off: no EDID, so nothing to derive
            # an address from, and no way to reach the bus at all.
            LOG.warning("still no CEC address - receiver is off or on another input")
            return None
        LOG.info("re-armed at %s", addr)
    return addr if configure() else None


def amp_is_on():
    out = cec("--to", AMP, "--give-device-power-status")
    if out is None:
        return None
    for line in out.splitlines():
        if "pwr-state:" in line:
            return "on" in line.split("pwr-state:", 1)[1]
    return None  # no reply: amp unreachable, treat as unknown


def other_device_is_active(mine):
    """True if some *other* device holds the active source.

    Nobody replying is the ordinary case - the receiver does not answer
    <Request Active Source> - and means there is nothing to tread on.
    """
    out = cec("--request-active-source")
    if out is None:
        return False
    for line in out.splitlines():
        if "phys-addr:" in line:
            addr = line.split("phys-addr:", 1)[1].strip()
            if addr and addr != mine:
                LOG.info("%s holds the active source, leaving it alone", addr)
                return True
    return False


# --------------------------------------------------------------------------
# Actions
# --------------------------------------------------------------------------
def wake():
    if not DO_WAKE:
        return False
    addr = ready()
    if not addr:
        return False
    # <Image View On> to the TV brings the display chain out of standby;
    # <Active Source> is what actually makes the receiver select our input,
    # because our physical address tells it which one we are behind.
    if tx_failed(cec("--to", "0", "--image-view-on")):
        LOG.warning("could not wake the display chain")
        return False
    if tx_failed(cec("--active-source", f"phys-addr={addr}")):
        LOG.warning("could not claim active source at %s", addr)
        return False
    LOG.info("woke the chain and claimed active source at %s", addr)
    return True


def standby():
    if not DO_STANDBY:
        return False
    addr = phys_addr()
    if not addr or addr == INVALID:
        return False          # nothing to send it with, and nothing to turn off
    if not configure():
        return False
    if amp_is_on() is False:
        LOG.info("amplifier already in standby")
        return False
    if other_device_is_active(addr):
        return False
    if tx_failed(cec("--to", AMP, "--standby")):
        LOG.warning("could not send the amplifier to standby")
        return False
    LOG.info("idle for %g min, sent the amplifier to standby", IDLE_MINUTES)
    return True


# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------
def ws_url():
    addr_f, port_f = STATE_DIR / "ws.addr", STATE_DIR / "ws.port"
    addr = addr_f.read_text().strip() if addr_f.exists() else "127.0.0.1"
    port = port_f.read_text().strip() if port_f.exists() else "3678"
    return f"ws://{addr}:{port}"


async def run():
    LOG.info("amp=%s idle=%g min wake=%s standby=%s",
             AMP, IDLE_MINUTES, DO_WAKE, DO_STANDBY)

    # Get on the bus up front rather than at the first play. A device that
    # holds a physical address but answers no polls is a ghost on the bus,
    # and the other devices keep looking for it.
    await asyncio.to_thread(configure)

    # "active" covers paused as well as playing: picking the device in Spotify
    # is itself a reason to switch the receiver over, and pausing is not a
    # reason to switch it away.
    active = False
    idle_since = time.monotonic()
    slept = False   # already sent to standby for this idle stretch

    async def idle_watch():
        nonlocal slept
        while True:
            await asyncio.sleep(IDLE_POLL_SECONDS)
            if active or slept or IDLE_MINUTES <= 0:
                continue
            if time.monotonic() - idle_since >= IDLE_MINUTES * 60:
                try:
                    await asyncio.to_thread(standby)
                except Exception as e:
                    LOG.warning("standby failed: %s", e)
                slept = True      # do not retry every 30 s until we play again

    asyncio.create_task(idle_watch())

    backoff = 1
    while True:
        try:
            url = ws_url()
            LOG.info("connecting to %s", url)
            async with websockets.connect(url, ping_interval=20) as ws:
                backoff = 1
                first = True
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if msg.get("type") != "playback_state":
                        continue
                    now_active = msg.get("status") in ("playing", "paused")
                    if first:
                        # Soloist replays the current state the moment we
                        # connect. A session that was already sitting there
                        # paused is not someone reaching for the device, and
                        # acting on it would claim active source - switching
                        # the receiver's input and waking the TV - on every
                        # daemon restart and every boot. Adopt that first
                        # state silently; act only on what changes after it.
                        first = False
                    elif now_active and not active:
                        slept = False
                        await asyncio.to_thread(wake)
                    elif not now_active and active:
                        idle_since = time.monotonic()
                    active = now_active
        except Exception as e:
            LOG.warning("websocket error: %s (retry in %ds)", e, backoff)
            # Soloist being gone is not the receiver's fault - do not touch it,
            # just stop counting this as playing.
            if active:
                idle_since = time.monotonic()
            active = False
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s",
                        stream=sys.stdout)
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
