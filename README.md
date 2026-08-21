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

### Forcing HDMI on breaks CEC, and CEC has to be re-armed

Forcing the connector is not free. `video=…D` puts it in
`DRM_FORCE_ON_DIGITAL`, and for a forced connector DRM skips the driver's
`->detect()` callback entirely — which is exactly where `vc4_hdmi` hands the
CEC adapter the physical address it reads out of the EDID.

Nothing looks wrong when this happens. The EDID still arrives, because that is
`->get_modes()`, which does still run: the modes are right, the ELD is right,
audio plays. Only CEC is dead, and it is dead in a way that reads as a wiring
fault rather than a software one — the adapter sits at physical address
`f.f.f.f`, never claims a logical address, and is simply absent from the bus:

```
$ cec-ctl -d /dev/cec0
	Physical Address           : f.f.f.f
	Logical Address Mask       : 0x0000
```

The adapter cannot just be told its address. `vc4_hdmi` does not advertise
`CEC_CAP_PHYS_ADDR`, so `cec-ctl --phys-addr` is refused outright:

```
The CEC adapter doesn't allow setting the physical address manually, ignore this option.
```

The address has to come from a real detect, so `scripts/cec-rearm.sh` borrows
one: it drops the force, waits for the detect to land an address, and puts the
force straight back. **The address survives the re-force** — that is the whole
trick, and it is why this works without giving up the forced connector.

`cec-rearm.service` runs it at boot, and `udev/99-cec-rearm.rules` runs it
again on every HDMI hotplug, because a receiver power cycle can drop the
address. The script is idempotent and takes a lock, so the change events its
own writes generate settle out after one no-op run.

Restoring the force is done through **debugfs**, not the sysfs `status` file.
`status` only understands `on`, which is plain `DRM_FORCE_ON` — that loses the
digital half of `DRM_FORCE_ON_DIGITAL` and takes the audio with it the next
time the receiver is switched off. `debugfs/…/force` accepts `digital`. If
debugfs is not mounted the script refuses to clear the force at all rather
than leave the connector in a state it cannot restore.

Check it with:

```bash
cec-ctl -d /dev/cec0 -S
```

which on this box should show the Pi at **2.3.0.0** as Playback Device 1,
under the Sony at 2.0.0.0, under the TV at 0.0.0.0. That address is read from
the receiver's EDID and encodes the physical wiring — the receiver is on the
TV's HDMI 2, and the Pi is on the receiver's HDMI 3. Move either cable and the
address changes on its own; nothing here hardcodes it.

### A physical address is not enough, the adapter needs a logical one too

Getting the physical address back is only half of being on the bus. The
physical address says *where* we are wired; a **logical** address is what lets
us transmit at all, and nothing gives us one by accident.

`cec-ctl` only calls `CEC_ADAP_S_LOG_ADDRS` when it is given a device type —
`--playback`, `--tv`, `--audio` and friends. Every invocation in this repo
used to omit one, so the adapter settled into a state that looks entirely
healthy at a glance:

```
	Physical Address           : 2.3.0.0     <- fine
	Logical Address Mask       : 0x0000      <- not on the bus
	Logical Addresses          : 0
```

The CEC core lets an unconfigured adapter transmit exactly one thing:
`<Image View On>` from the Unregistered address to the TV. Everything else —
every `--to 5` power query, every `<Standby>`, every `<Active Source>` — is
refused with `ENONET`. And `cec-ctl` **exits 0 whether the message went out or
never left**, so nothing in the exit status gives it away; the only honest
signal is the `Tx, …` line it prints, which is why `soloist-cec.py` reads it
rather than the return code.

The visible symptom was not the receiver ignoring Soloist. It was the LG TV
remote and the Shield remote both losing the ability to change the Sony's
volume, while the amplifier itself was perfectly healthy — `System Audio Mode`
on, unmuted, and obeying a `<User Control Pressed>` sent by hand. What the Pi
was doing to the bus was sitting at 2.3.0.0 as a device that answers no polls,
a ghost the other devices keep looking for. Giving it a logical address
restored volume control immediately. Strictly, the causal chain from ghost to
lost volume routing was never proven — but a device that holds a physical
address and answers nothing is wrong regardless, and it is the only thing that
changed.

So `scripts/cec-rearm.sh` and `cec/soloist-cec.py` both ensure the adapter is
configured before they rely on it:

```bash
cec-ctl -d /dev/cec0 --playback -o "Hi-Fi System"
```

`--playback` claims logical address **4**, falling back to 8 then 11. It will
never take 5, so it cannot collide with the amplifier we are trying to talk
to. The OSD name is capped at 14 characters by the spec.

This is runtime kernel state and does **not** survive a reboot, which is why
it is done in the boot path rather than once by hand. It does survive the
physical address coming and going within a boot: `CEC_ADAP_S_LOG_ADDRS` stores
the request, and the core re-claims automatically whenever a valid physical
address turns up. `cec-rearm.sh` therefore configures before its early exits
too, so the receiver-is-off path still leaves the claim armed for later.

### What CEC is used for

`cec/soloist-cec.py` watches the same WebSocket the screen does and does the
two things you would otherwise pick up the remote for:

- **Starting a Connect session switches the receiver over.** On the first
  `playing` *or* `paused` state — picking "Hi-Fi System" in Spotify is itself
  reason enough — it sends `<Image View On>` to the TV and broadcasts
  `<Active Source>` with its own physical address. The receiver reads 2.3.0.0
  as "behind my HDMI 3" and selects that input, so choosing the device in
  Spotify is the only action needed to get sound.
- **Half an hour of nothing sends the amplifier to standby.**
  `SOLOIST_CEC_IDLE_MINUTES` in the env file, 0 to disable.

Standby is addressed to logical address 5, the Audio System, and **not**
broadcast. A broadcast `<Standby>` takes the TV down with the amplifier, and
the TV is not ours to switch off — someone may be watching it with the music
paused. For the same reason the idle timer checks first whether another device
holds the active source, so leaving Spotify paused does not power off the
amplifier out from under the SHIELD on the next input.

That the round trip works at all is worth stating, because it is the thing
that would quietly not: **the CEC address survives the amplifier going into
standby.** The receiver drops the HDMI hotplug line when it powers down, which
would normally take the EDID and the CEC address with it — and since a forced
connector never runs another detect, the address would never come back and
nothing could ever wake it again. It survives precisely *because* the
connector is forced: with `->detect()` out of the picture, there is nothing
left to invalidate it. Verified both directions on the hardware — amplifier to
standby, address still `2.3.0.0`, wake on the next play.

If the address has gone anyway, the daemon runs `cec-rearm.sh` itself before
giving up, since the usual reason to have lost it is the very power cycle it
is reacting to.


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

The composition is a centred column — cover, title under it, then artist and
album on their own lines, each a step smaller and a step greyer than the one
above.

No X11, no Wayland, no browser. A Chromium kiosk is the usual way to do this
and it will not fit here — the board has ~400 MB of RAM and one small core.
Rendering to `/dev/fb0` with PIL costs **3.3 % of one core and 8.9 MB RSS**,
measured over 90 s of a paused track.

Details that matter:

- **`/dev/fb0` is RGB565** (16 bpp, 1280x720, unpadded scanlines). The script
  reads the real geometry and pixel format via `FBIOGET_VSCREENINFO` and
  refuses to run against anything else rather than painting garbage.
- **Colour is dithered** with a 4x4 Bayer matrix before packing to RGB565.
  Truncating 8-bit colour posterises smooth tone into visible rings on a large
  TV. Pure black is unaffected, so the background stays at 0,0,0.
- **There is no progress bar and no clock.** Both were a second's worth of
  moving pixels in a fixed place, which is the shape of burn-in, and neither
  told you anything the music does not. Dropping them also took out the local
  position extrapolation that fed it — the screen repaints when the track
  changes, and otherwise only for the pixel shift.
- **The whole frame walks a circle** of radius 8 px, one of 16 steps every 45
  seconds, so a full cycle takes 12 minutes. Nothing static — the artwork's
  edge above all — sits on the same subpixels for long. The excursion is 16 px
  end to end: invisible from the sofa, and well past a pixel.
- **The state is a glyph, not a word.** Paused dims the cover to 38 % and puts
  a pause bar in the middle of it; buffering spins a tapered ring there. A
  word set in caps is one more thing to read from the sofa, and a shape lands
  before you focus on it. Nothing moves in the layout when the state changes,
  because the glyph sits *on* the artwork rather than in the text.
- **The spinner costs 60 rows, not a frame.** Scanlines are unpadded, so any
  run of whole rows is one contiguous region of the device: the ring is
  written on its own with a positioned `pwrite` at 23 ms a frame instead of
  the 180 ms a full repaint takes. The Bayer matrix is phase-shifted to match
  the band's first row, or the dither inside it would not line up with the
  dither around it.
- **Lines sit on their baselines, never on their ink.** Ink bounds move with
  the string, so a title that happens to have no descender would otherwise
  shove everything under it around. The title is the one line worth reading
  from across the room: it gives up a tenth of its size at most before it
  takes a *second* line instead, split as evenly as the words allow, and the
  gap under it is measured from its last baseline either way.
- **Every measure is a fraction of the screen height**, so 720p and 1080p get
  the same composition rather than the same pixel sizes.
- **Nothing is tinted from the artwork.** The cover is the only saturated
  thing in the frame; a second colour sampled out of it only ever competed
  with it. One neutral grey ramp on black instead. A track with no cover at
  all gets a note drawn on a near-black square, because a flat grey panel
  reads as a failure and this reads as a decision.
- **A daemon that goes away does not blank the screen.** A soloist restart is
  a two-second blip, so the last frame is held for 20 seconds before the
  screen admits it does not know what is playing and falls back to *Ready*.
  Reconnection is attempted every 5 seconds and logged once per outage, not
  once per attempt.
- **The album art cache is capped** at the 300 most recently used covers
  (`~/.cache/soloist-display`), touched on every hit. Downloads are decoded
  before they are cached and renamed into place after, so a cut connection
  cannot leave an entry that fails to open forever.

#### The font

The screen is set in **Nunito Sans** — Medium for the title, Regular for
artist and album. Raspberry Pi OS ships it and sets its own desktop in it, and
`install.sh` pulls `fonts-nunito-sans` in regardless.

Spotify sets *its* interface in **Circular** (Lineto), latterly in **Spotify
Mix**. Both are licensed, neither is redistributable, so this repo does not
ship either one and cannot fetch them for you — but it will use them if you
have them. The script picks the first family it finds on the font path,
preferring, in order: Spotify Mix, Circular Sp, Circular Std, **Nunito
Sans**, Montserrat, Inter, DejaVu. If you own a licence, drop the files
anywhere under `~/.local/share/fonts` and restart the display service:

```bash
systemctl --user restart soloist-display.service
```

To go back to the geometric look without a licence, delete Nunito Sans from
the list in `display/soloist-display.py` and Montserrat picks it up — the
closest free stand-in for Circular, with circular bowls, a double-storey `a`
and a single-storey `g`.

Matching is on squashed filenames, so `CircularStd-Book.otf` and `Circular Std
Book.ttf` both work. A **variable** font names its weights inside the file
rather than in the filename — Nunito Sans ships as one
`NunitoSans-VariableFont_…ttf` holding everything from ExtraLight to Black —
so those files are opened and their named instances matched instead. This is
not optional politeness: a variable font opens at its *default* instance, and
Nunito Sans defaults to ExtraLight, which at 2 m is barely there. The journal
logs the family and the weight each role landed on at startup:

```
fonts: nunitosans (title Medium, body Regular)
```

The screen commits to **one** family for every line: a family that is
present but missing a weight repeats its own faces rather than borrowing the
next family's, because two designs on one screen reads as a bug, not a
fallback. DejaVu is the last resort — `install.sh` guarantees it, and a plain
screen beats no screen.

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
systemctl --user status soloist-cec.service       # receiver control
cec-ctl -d /dev/cec0 -S                      # what is on the CEC bus
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

**CEC does nothing.** Check *both* addresses — they fail independently:

```bash
cec-ctl -d /dev/cec0 | grep -E "Physical Address|Logical Address"
```

`Physical Address: f.f.f.f` means the re-arm never landed. Re-arm it by hand
with `sudo ./scripts/cec-rearm.sh`. Exit code 75 means the receiver was off or
on another input, so there was no EDID to take an address from; switch it on
and try again. `systemctl status cec-rearm.service` shows what happened at
boot.

`Logical Address Mask: 0x0000` with a valid physical address means the adapter
is not on the bus and can transmit nothing but `<Image View On>`. That is the
failure described above; `sudo ./scripts/cec-rearm.sh` fixes it too, or by
hand with `cec-ctl -d /dev/cec0 --playback -o "Hi-Fi System"`. A healthy box
shows mask `0x0010`, logical address 4.

**Neither the TV remote nor the Shield remote can change the volume.** Prove
where it breaks before touching anything, because the amplifier is usually
innocent:

```bash
cec-ctl -d /dev/cec0 -s --to 5 --give-system-audio-mode-status
cec-ctl -d /dev/cec0 -s --to 5 --give-audio-status
```

`sys-aud-status: on` and a volume that moves when you send
`--to 5 --user-control-pressed ui-cmd=volume-up` means the receiver is fine
and something upstream is not reaching it — check the Pi's logical address
first.

**The receiver does not switch input when playback starts.** Check
`journalctl --user -u soloist-cec.service` for "claimed active source". Sony
calls the setting that makes it obey **Control for HDMI**, and it has to be on
at the receiver — CEC is advisory and a receiver with it switched off will
take the message and ignore it. `SOLOIST_CEC_WAKE=0` turns the behaviour off
at this end.

**The receiver switches itself off while I am using it.** The idle timer only
fires when nothing has played for `SOLOIST_CEC_IDLE_MINUTES` and no other
device claims the active source; set `SOLOIST_CEC_STANDBY=0` to stop it
entirely.
