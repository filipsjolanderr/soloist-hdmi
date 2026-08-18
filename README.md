# soloist-hdmi

Headless [Spotify Soloist](https://developer.spotify.com/documentation/soloist)
Spotify Connect endpoint on a Raspberry Pi Zero 2 W, feeding a Sony AV receiver
over HDMI. Starts at boot, no login or display required.

## What this box is

| | |
|---|---|
| Host | `hifi` — Raspberry Pi Zero 2 W Rev 1.0 |
| OS | Debian 13 (trixie), aarch64, glibc 2.41 |
| Audio out | `vc4hdmi` → HDMI → Sony AV receiver |
| PipeWire node | `alsa_output.platform-3f902000.hdmi.hdmi-stereo` |
| Connect name | `Receiver` |

This is the exact platform Spotify lists as the primary test target for the
ARMv8/AArch64 build, so no compatibility workarounds are needed.

## Install

```bash
git clone <this-repo> ~/soloist-hdmi
cd ~/soloist-hdmi
./install.sh
```

Then put your API key in `~/.config/soloist-hdmi/env` and:

```bash
systemctl --user restart soloist.service
```

Pair once by opening Spotify on any device on the same LAN and picking
**Receiver** from the device picker. The session is stored in
`~/.local/state/soloist`, so it survives restarts and reboots.

## Design notes

Things here that are deliberate, and will bite if changed casually.

### The API key is not in this repo

It lives in `~/.config/soloist-hdmi/env`, mode `0600`, loaded via
`EnvironmentFile=`. The path is **not** `~/.config/soloist/` — for a *user*
unit, systemd resolves `StateDirectory=` to `~/.local/state/soloist` but will
silently symlink it onto `~/.config/soloist` if that directory exists, dropping
the secret into the same directory Soloist writes state and crash dumps into.

Note that Soloist only accepts the key as a command-line flag, so it is visible
in `ps` and in `systemctl status` output to any local user. On a single-user
appliance that is acceptable; it is worth knowing before adding other accounts.

### HDMI is forced on

`/boot/firmware/cmdline.txt` carries:

```
video=HDMI-A-1:1280x720@60D
```

Without this, powering the receiver off drops the HDMI hotplug line, the
`vc4hdmi` ALSA card loses its sink, PipeWire tears the sink down, and the
Connect device vanishes until reboot. The trailing `D` forces digital (HDMI
rather than DVI) signalling, which is what keeps audio alive.

`1280x720@60` is the receiver's own EDID preferred timing — it does *not*
advertise 1080p as preferred, so do not "upgrade" this to 1920x1080.

Original boot files are backed up as `/boot/firmware/*.bak-soloist`.

### 44.1 kHz, not 48 kHz

`pipewire/10-hifi-hdmi.conf` sets the graph to 44.1 kHz with 48 kHz allowed.
Spotify streams at 44.1 kHz and PipeWire defaults to 48 kHz, which would
resample every track. The receiver's EDID advertises LPCM at 32/44.1/48/88.2/
96/176.4/192 kHz, so 44.1 passes through untouched.

The quantum is raised to 2048. Latency is irrelevant for one-way playback and
the Pi Zero 2 W has one small core and ~400 MB RAM; fewer wakeups means no
xruns.

### Volume is 100 %

`SOLOIST_INITIAL_VOLUME=100` and the PipeWire sink sits at 1.00, so no digital
attenuation is applied and the receiver's own volume control does the work.

**Turn the receiver down before first playback.**

Lower `SOLOIST_INITIAL_VOLUME` in the env file if you would rather have a
quieter default, at the cost of some bit depth.

### Builds expire after 90 days

Soloist binaries stop working 90 days after their build date and exit with
code 10. On a headless box that means the music silently stops one day.

- `soloist-update.timer` runs weekly (`Persistent=true`, so a Pi that was off
  catches up on boot) and installs the current build.
- `RestartPreventExitStatus=10` in the unit stops systemd from pointlessly
  restart-looping an expired binary.

Force an update by hand with:

```bash
./scripts/update-soloist.sh
```

### Lingering

`loginctl enable-linger` is required — without it these user services would
only run while a login session exists, which never happens on a headless box.

## Operating it

```bash
systemctl --user status soloist.service      # is it up
journalctl --user -u soloist.service -f      # follow logs
systemctl --user restart soloist.service     # restart
soloist ctl --help                           # local playback control
```

A WebSocket API is bound to `127.0.0.1:3678` (localhost only, deliberately) for
scripted control. The resolved port is also written to
`~/.local/state/soloist/ws.port`.

## Troubleshooting

**Device missing from the Spotify picker.** Both ends must be on the same LAN
with client isolation off on the router/AP. Check `systemctl --user status
soloist.service` for login errors.

**Exit code 10.** The build expired. Run `./scripts/update-soloist.sh`. Note
the unit will be in `failed` state; the script resets it.

**Playback runs but no sound.** Confirm the sink exists and is unmuted:

```bash
wpctl status
./scripts/find-hdmi-node.sh
```

If the node name has changed, update `SOLOIST_PIPEWIRE_NODE` in
`~/.config/soloist-hdmi/env`.

**Nothing after a receiver power cycle.** Verify `video=HDMI-A-1:1280x720@60D`
is still on the single line in `/boot/firmware/cmdline.txt`.
