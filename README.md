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
| Connect name | `Hi-Fi System` |
| TV output | 1280x720 now-playing screen on `/dev/fb0` |

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
**Hi-Fi System** from the device picker. The session is stored in
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

### Volume normalization is not controllable here

Soloist exposes **no** normalization setting. Verified three ways:

- `soloist --help` has no normalization flag.
- `soloist ctl` has no normalization command; the daemon's full WebSocket
  command vocabulary is play / pause / next / prev / seek / volume / shuffle /
  repeat / add_to_queue / activate / deactivate and nothing else.
- `soloist ctl now --json` reports `options` as shuffle, repeat,
  playback_speed and modes only, with no normalization field and no
  normalization entry in `available_actions`.

The engine does contain the machinery (`enable_normalization`,
`NormalizerSetupImpl`, `normalize_level`, `PeakLimiter`) driven by the
`audio.normalize_v2` preference, but that value arrives with the account's
product state from Spotify and is not cached on disk here — nothing in this
repo, and no local config file, can set it.

So it is an **account-level setting**: change Audio Normalization in the
Spotify app's playback settings. Anything applied by normalization happens
inside Soloist before audio reaches PipeWire, so it cannot be undone
downstream.

Everything downstream of Soloist is already transparent: no resampling
(negotiated `S32P / 44100 / 2ch`), sink volume at 1.00, no filters in the
graph.

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

### The TV screen

`display/soloist-display.py` paints the album art, title, artist and album
straight to the Linux framebuffer, driven by Soloist's WebSocket API.

No X11, no Wayland, no browser. A Chromium kiosk is the usual way to do this
and it will not fit here — the board has ~400 MB of RAM and one small core.
Rendering to `/dev/fb0` with PIL costs **3.3 % of one core and 12.6 MB RSS**.

Details that matter:

- **`/dev/fb0` is RGB565** (16 bpp, 1280x720, unpadded scanlines). The script
  reads the real geometry and pixel format via `FBIOGET_VSCREENINFO` and
  refuses to run against anything else rather than painting garbage.
- **Colour is dithered** with a 4x4 Bayer matrix before packing to RGB565.
  Truncating 8-bit colour posterises smooth tone into visible rings on a large
  TV. Pure black is unaffected, so the background stays at 0,0,0.
- **There is no progress bar and no clock.** Both were a second's worth of
  moving pixels in a fixed place, which is the shape of burn-in, and neither
  told you anything the music does not. Dropping them also took out the
  per-second partial framebuffer write and the local position extrapolation
  that fed it — the screen now only repaints when the track changes.
- **The whole frame walks a circle** of radius 8 px, one of 16 steps every 45
  seconds, so a full cycle takes 12 minutes. Nothing static — the artwork's
  edge above all — sits on the same subpixels for long. The excursion is 16 px
  end to end: invisible from the sofa, and well past a pixel.
- **Text is bottom-aligned to the artwork.** The title/artist/album stack sits
  on the cover's bottom edge, aligned on the last line's *baseline* rather
  than its ink, so the block does not jump when a title happens to have no
  descender.
- **Nothing is tinted from the artwork.** The cover is the only saturated
  thing in the frame; a second colour sampled out of it only ever competed
  with it. One neutral grey ramp on black instead.

#### The font is a stand-in

Spotify sets its interface in **Circular** (Lineto), latterly in **Spotify
Mix**. Both are licensed, neither is redistributable, so this repo does not
ship either one and cannot fetch them for you.

It will use them if you have them. The script picks the first family it finds
on the font path, preferring, in order: Spotify Mix, Circular Sp, Circular
Std, then **Montserrat** as the stand-in — the closest free geometric sans,
with circular bowls, a double-storey `a` and a single-storey `g`. If you own a
licence, drop the files anywhere under `~/.local/share/fonts` and restart the
display service:

```bash
systemctl --user restart soloist-display.service
```

Matching is on squashed filenames, so `CircularStd-Book.otf` and `Circular Std
Book.ttf` both work. The journal logs which family won at startup. The screen
commits to **one** family for all three lines: a family that is present but
missing a weight repeats its own faces rather than borrowing the next
family's, because two designs on one screen reads as a bug, not a fallback.
DejaVu is the last resort — `install.sh` guarantees it, and a plain screen
beats no screen.

The framebuffer console must be unbound first or it repaints the login console
over the display; `scripts/fbcon.sh release` does this and the service calls it
via `ExecStartPre`, restoring it on stop. To get the console back on the TV by
hand:

```bash
sudo ~/soloist-hdmi/scripts/fbcon.sh restore
```

### Lingering

`loginctl enable-linger` is required — without it these user services would
only run while a login session exists, which never happens on a headless box.

## Operating it

```bash
systemctl --user status soloist.service      # is it up
systemctl --user status soloist-display.service
journalctl --user -u soloist.service -f      # follow logs
systemctl --user restart soloist.service     # restart
./scripts/soloistctl status                  # playback control
./scripts/soloistctl now                     # what's playing
```

Use `scripts/soloistctl`, not bare `soloist ctl`. The daemon runs with
systemd's `StateDirectory=`, so its pid and WebSocket files live in
`~/.local/state/soloist`, while `soloist ctl` defaults to looking in
`~/.local/share/soloist` — bare `ctl` reports `not running` even mid-playback.
The wrapper just pins `-D` to the right directory.

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

**TV shows the terminal instead of the now-playing screen.** The display
service is not running, or fbcon was rebound. Check
`systemctl --user status soloist-display.service`.

**TV screen is frozen on an old track.** The WebSocket connection dropped; the
script reconnects with backoff, so check the journal for `websocket error`.

**Nothing after a receiver power cycle.** Verify `video=HDMI-A-1:1280x720@60D`
is still on the single line in `/boot/firmware/cmdline.txt`.
