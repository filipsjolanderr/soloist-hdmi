# soloist-hdmi — `aux` branch

Headless [Spotify Soloist](https://developer.spotify.com/documentation/soloist)
Spotify Connect endpoint on a Raspberry Pi 4, feeding a single speaker over the
3.5mm analog jack. Starts at boot, no login or display required.

This is the `aux` branch. `main` is the Pi Zero 2 W box that feeds a Sony AV
receiver over HDMI; the two share the daemon, the update timer and the control
wrapper, and differ in everything to do with output.

## What this box is

| | |
|---|---|
| Host | `hifi2` — Raspberry Pi 4 Model B Rev 1.5 |
| OS | Debian 13 (trixie), aarch64 |
| Audio out | `bcm2835 Headphones` → 3.5mm jack → **right channel only** |
| PipeWire node | `soloist_mono_right` (a mono sink in front of the hardware) |
| Connect name | `Hi-Fi System` |
| TV output | none |

## What is not here

Three things from `main` are gone, and none of them are coming back on analog:

- **`soloist-display`.** The now-playing screen paints to `/dev/fb0` over HDMI.
  Nothing is plugged into HDMI here, and with no connector attached the vc4
  driver publishes no framebuffer at all — there is no `/dev/fb0` to paint to.
- **`soloist-cec` and `cec-rearm`.** CEC is a pair of wires in the HDMI
  connector. With no HDMI link there is no bus, no receiver to wake, and no
  input to switch. The udev rule and the `cec-rearm` system unit go with them.
- **The forced connector.** `video=HDMI-A-1:1280x720@60D` in `cmdline.txt`
  existed to stop the `vc4hdmi` ALSA card disappearing when the receiver powered
  off, and forcing it is what broke CEC and required the whole re-arm dance in
  the first place. The `bcm2835 Headphones` card is on the SoC and is always
  present, so `cmdline.txt` is left completely untouched on this box.

The single most useful thing to understand about this branch is that the entire
first half of `main`'s README describes a problem that analog output does not
have.

## Install

```bash
git clone -b aux <this-repo> ~/soloist-hdmi
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

Unchanged from `main`. It lives in `~/.config/soloist-hdmi/env`, mode `0600`,
loaded via `EnvironmentFile=`. The path is **not** `~/.config/soloist/` — for a
*user* unit, systemd resolves `StateDirectory=` to `~/.local/state/soloist` but
will silently symlink it onto `~/.config/soloist` if that directory exists,
dropping the secret into the same directory Soloist writes state and crash dumps
into.

Soloist only accepts the key as a command-line flag, so it is visible in `ps`
and in `systemctl status` output to any local user. On a single-user appliance
that is acceptable; it is worth knowing before adding other accounts.

### 100 % volume is +4 dB on this card, not unity

This is the one place where porting `main`'s design *literally* breaks it.

`main` runs the sink at 1.00 and `SOLOIST_INITIAL_VOLUME=100` so that no digital
attenuation is applied and the receiver's analog volume does the work. That
reasoning is sound and it carries over — but the bcm2835 hardware mixer does
not behave like the HDMI one. Its `PCM` control runs:

```
$ amixer -c 0 sget PCM
  Limits: Playback -10239 - 400
  Mono: Playback 400 [100%] [4.00dB] [on]
```

from -102.39 dB to **+4.00 dB**, and WirePlumber maps sink volume 1.00 onto the
top of that range. So "no attenuation" ported as-is is +4 dB of digital *boost*
ahead of an 11-bit-ish PWM DAC, and full-scale material clips.

The mapping is `dB = 4.00 + 60·log₁₀(volume)`, which is worth knowing only
because it makes clear there is no volume setting that is both 1.00 and 0 dB.
So the fix is not to pick a magic number, it is to take the hardware control out
of the loop: `wireplumber/50-aux-softmixer.conf` sets `api.alsa.soft-mixer`, so
volume is applied in software — a no-op multiply at 1.00 — and the ALSA control
is never written. `install.sh` pins it at `0dB` and runs `alsactl store` so
`alsa-restore` puts it back at every boot.

**Turn the amplifier down before first playback.** That part is unchanged.

#### Two matches, on two different keys, and neither is redundant

`50-aux-softmixer.conf` looks like it matches the same card twice. It does, and
both are load-bearing, because volume reaches the card by two separate paths:

- the **node**'s own mixer, and
- the **device**'s route, which is set from `device.routes.default-sink-volume`.

Setting `api.alsa.soft-mixer` on the node alone leaves the route path intact,
and the route path is the one that actually bites — the node prop reads back as
`True` while the control still snaps to +4 dB on every WirePlumber restart,
which is a convincing way to think the setting did nothing.

The two objects then have to be matched on *different* keys. `alsa.card_name` is
not yet populated on the device object when device rules are evaluated, only
`api.alsa.card.name` is; match the device on `alsa.card_name` and it silently
does not match. Matching on the card's ALSA name rather than the node name also
keeps this file free of the SoC address, so it is the one config here that needs
no per-board substitution.

`device.restore-routes = false` looks like it should help and does not: with
route restore off WirePlumber applies `device.routes.default-sink-volume`
instead, which defaults to exactly the 0.40 you were trying to get away from.

### Mono, on the right channel only

There is one speaker and it is wired to the right side of the jack.

`pipewire/20-mono-right.conf` publishes a sink with a single `MONO` channel and
loops it into the hardware sink positioned at `FR`. Soloist targets that sink by
name, so:

- **Both source channels survive.** PipeWire's channel mixer downmixes stereo
  into the mono sink at `0.5·FL + 0.5·FR`. Nothing is dropped, and because the
  coefficients sum to 1.0 a centred mix cannot clip. Sending the hardware sink
  a stereo stream and simply not connecting the left plug would instead throw
  away everything panned left.
- **`stream.dont-remix = true` is what makes it one channel.** Without it
  PipeWire helpfully upmixes the single channel back out to both `FL` and `FR`,
  which is the opposite of the point. With it the link is positional, `FR` to
  `FR`, and `FL` is left as digital silence.
- **Targeting by node name survives a default-sink change.** If an HDMI sink
  ever appears — plug a monitor in — WirePlumber may move the system default to
  it. Soloist is pinned to `soloist_mono_right` and does not care.

Verified end to end rather than by reading the config, by playing a stereo file
with 440 Hz on the left and 997 Hz on the right into the mono sink and capturing
the hardware sink's monitor:

| channel | RMS | 440 Hz | 997 Hz |
|---|---|---|---|
| left | `-inf` | `-inf` | `-inf` |
| right | -13.0 dB | -13.0 dB | -13.0 dB |

Left is digital silence and right carries both tones, each at -13.0 dB — the
tones went in at 0.45 amplitude (-6.9 dB) and 0.5·FL + 0.5·FR predicts
-12.96 dB.

To go back to two speakers, delete
`~/.config/pipewire/pipewire.conf.d/20-mono-right.conf`, set
`SOLOIST_PIPEWIRE_NODE` to the hardware sink from `scripts/find-audio-node.sh`,
and restart pipewire and soloist.

### 44.1 kHz, not 48 kHz

As on `main`, and for the same reason: Spotify streams at 44.1 kHz and PipeWire
defaults to 48 kHz, which would resample every track. The bcm2835 device accepts
44.1 kHz directly — `/proc/asound/card0/pcm0p/sub0/hw_params` reports
`rate: 44100` with no plug layer interposed — so the resampler drops out.

Unlike the HDMI path this is **not** a claim of an untouched signal all the way
to the speaker. `main` could point at the receiver's EDID and say 44.1 passes
through; here the analog stage is a PWM DAC driven by firmware, and what it does
downstream is neither visible nor controllable from Linux. `alsa.resolution_bits`
reads 16, the real figure is lower, and none of it is ours to change. This
setting removes the one resample we can see.

The quantum is still raised to 2048 but the *reason* from `main` does not apply:
that was a Pi Zero 2 W with one small core and ~400 MB of RAM avoiding xruns.
This is a 4-core Pi 4 with 4 GB under no pressure. It is kept only because
latency is irrelevant for one-way playback, so fewer wakeups is free.

### If you care about the sound, use a USB DAC

The 3.5mm jack on a Pi is a PWM output filtered by a handful of passives. It is
fine for a kitchen speaker, which is what this box is. A USB DAC would appear as
another ALSA card and everything here would carry over — re-run
`scripts/find-audio-node.sh`, point `node.target` in `20-mono-right.conf` at the
new sink, and drop `50-aux-softmixer.conf` if the DAC's mixer has a sane range.
Not done, because it is not the box that was asked for.

### Builds expire after 90 days

Unchanged from `main`. Soloist binaries stop working 90 days after their build
date and exit with code 10. On a headless box that means the music silently
stops one day.

- `soloist-update.timer` runs weekly (`Persistent=true`, so a Pi that was off
  catches up on boot) and installs the current build.
- `RestartPreventExitStatus=10` in the unit stops systemd from pointlessly
  restart-looping an expired binary.

Force an update by hand with:

```bash
./scripts/update-soloist.sh
```

### Volume normalization is not controllable here

Unchanged from `main`, and re-checked against the build installed here: Soloist
exposes no normalization flag, no `ctl` command and no field in
`soloist ctl now --json`. It is an **account-level** setting — change Audio
Normalization in the Spotify app's playback settings. Anything it applies
happens inside Soloist before audio reaches PipeWire, so it cannot be undone
downstream.

### Lingering

`loginctl enable-linger` is required — without it these user services would only
run while a login session exists, which never happens on a headless box.

## Operating it

```bash
systemctl --user status soloist.service      # is it up
journalctl --user -u soloist.service -f      # follow logs
systemctl --user restart soloist.service     # restart
./scripts/soloistctl status                  # playback control
./scripts/soloistctl now                     # what's playing
./scripts/find-audio-node.sh                 # list sinks
```

Use `scripts/soloistctl`, not bare `soloist ctl`. The daemon runs with systemd's
`StateDirectory=`, so its pid and WebSocket files live in
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

**Exit code 10.** The build expired. Run `./scripts/update-soloist.sh`. Note the
unit will be in `failed` state; the script resets it.

**Playback runs but no sound.** Check the mono sink exists and soloist is
pointed at it:

```bash
./scripts/find-audio-node.sh
wpctl status
```

If `soloist_mono_right` is missing, the loopback module failed to load — check
that `~/.config/pipewire/pipewire.conf.d/20-mono-right.conf` had
`__SINK_NODE__` substituted and names a sink that exists.

**Sound is quiet, distorted, or clipping.** Check the hardware control is at
0 dB and not at either end of its range:

```bash
amixer -c 0 sget PCM
```

`[100%] [4.00dB]` means the soft-mixer rule is not taking effect and WirePlumber
is driving the control — confirm both matches in
`~/.config/wireplumber/wireplumber.conf.d/50-aux-softmixer.conf` are present,
restart wireplumber, then re-pin with `amixer -c 0 sset PCM 0dB && sudo alsactl
store`.

**Sound in the wrong channel, or in both.** Confirm the routing rather than
guessing, by capturing the hardware sink's monitor while something plays:

```bash
pw-record --target "$(./scripts/find-audio-node.sh --analog)" -c 2 /tmp/cap.wav
```

Both channels carrying signal means `stream.dont-remix` was lost from
`20-mono-right.conf`. The left channel carrying signal instead means
`audio.position` in `playback.props` says `FL`.
