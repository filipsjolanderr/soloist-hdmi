# soloist-hdmi — `aux` branch

A two-room Snapcast hi-fi built from two Raspberry Pis. Spotify Connect and
Bluetooth in, two synchronised speakers out, no login or display required at
either end.

`main` is the original single box: one Pi Zero 2 W running Soloist straight into
a Sony receiver over HDMI. This branch grew that into a server and two clients,
and most of `main`'s hard-won HDMI notes no longer apply — see *What is gone*.

## The system

```
  hifi2 — Pi 4 Model B, 192.168.0.134 — SERVER
  ┌──────────────────────────────────────────────────────────┐
  │  Soloist (Spotify Connect)                                │
  │      └─► snapcast_spotify ──► /run/snapcast/spotify.fifo  │
  │                                       │                   │
  │                                  snapserver               │
  │                          44100:16:2, FLAC, :1704          │
  │                          control :1705, web :1780         │
  └───────────┬──────────────────────────────┬────────────────┘
              │                              │
     snapclient (local)              snapclient over wifi
     aux_mono_right → FR             hifi — Pi Zero 2 W, .162
     USB DAC, one speaker            HDMI → Sony receiver
                                     + now-playing screen on /dev/fb0
                                     + receiver wake/standby over CEC

  Google Nest Hub ──Bluetooth A2DP──► aux_mono_right   (bypasses Snapcast)
```

| | hifi2 | hifi |
|---|---|---|
| Board | Pi 4 Model B Rev 1.5 | Pi Zero 2 W Rev 1.0 |
| Role | server + client | client |
| Output | USB DAC → 3.5mm, mono → right | HDMI → Sony receiver |
| Also runs | Soloist, Bluetooth sink | TV screen, CEC |

## What is gone

Three things from `main` that this branch does not have, and why:

- **Soloist on the Zero.** hifi2 is the Connect endpoint now. Two boxes
  advertising `Hi-Fi System` would appear twice in Spotify's picker and race for
  the session — which is exactly what the logs showed while both were up.
- **The Zero's local Soloist WebSocket.** The screen and the CEC daemon read it.
  They now read snapserver's control API instead, which is a smaller change than
  it sounds and a more correct source: they react to whatever the *system* is
  playing rather than to one particular endpoint.
- **The forced HDMI connector, on hifi2 only.** `video=HDMI-A-1:1280x720@60D`
  existed to stop the `vc4hdmi` ALSA card vanishing when the receiver powered
  off. hifi2 outputs analog and has nothing plugged into HDMI, so its
  `cmdline.txt` is untouched. **The Zero still needs it**, and still needs the
  whole CEC re-arm dance in `main`'s README that follows from it.

## Design notes

### Google Cast is not possible, and Bluetooth is the answer anyway

The Hub cannot cast to this system. Google Cast authenticates the *receiver*
with a Google-issued device certificate that senders validate, so it cannot be
implemented outside certified hardware — every open-source Cast project
(`pychromecast`, `catt`, `mkchromecast`, `node-castv2`) is a **sender**. No
amount of work makes a Pi appear in the Google Home app as a cast target.

What does work, and is what the Hub is actually for here: a Nest Hub will use a
**paired Bluetooth speaker** as its audio output. hifi2 advertises itself as one
— class `0x200414`, Audio/Video major, Loudspeaker minor, which is what makes
the Hub offer to pair with it at all rather than seeing a generic computer.

**Bluetooth deliberately bypasses Snapcast.** It could have been another
snapserver source scoped to one group, and that would be tidier. Measured
against the tone test, the Snapcast path adds ~1.0 s of buffering versus ~0.16 s
routing straight to the sink. That is fine for music and wrong for a Hub that
also speaks timers and answers questions. PipeWire mixes the two, so the Hub
talks *over* whatever Snapcast is playing instead of fighting it for the output.

`scripts/bt-audio-route.py` makes the connection, because PipeWire will not:
sources are not linked to sinks automatically (PulseAudio's
`module-bluetooth-policy` used to do this; WirePlumber ships no equivalent). It
matches `bluez_input.*` specifically rather than "capture the default source",
which would grab a future USB microphone and feed it to the speaker.

Pairing is `NoInputNoOutput` — there is no keypad here to confirm a passkey on,
which is how any standalone BT speaker behaves. It also means anything in range
can pair while the adapter is discoverable. Once the Hub is paired:

```bash
bluetoothctl -- discoverable off
```

### 0 dB is the target, and "100 %" is not reliably 0 dB

The speaker is now driven by a **USB DAC** (`HI-XCESS_618 USB Audio`) rather than
the Pi's own 3.5mm jack. The built-in jack still works as a fallback, so both are
described here - and they behave *oppositely*, which is exactly why `install.sh`
asks for `0dB` by name and never for a percentage:

| card | mixer range | what "100 %" means |
|---|---|---|
| bcm2835 built-in jack | −102.39 .. **+4.00 dB** | +4 dB of digital **boost**, clips |
| HI-XCESS USB DAC | −23.00 .. **0.00 dB** | exactly 0 dB, correct |

So on the built-in jack, running the sink at 1.00 - `main`'s stated design of no
digital attenuation, letting the amplifier do the work - actually clips. On the
USB DAC the identical setting is right. `amixer sset PCM 0dB` is correct on both,
and the card index is derived from whichever sink is in use rather than assumed
to be card 0, because a USB DAC does not land there.

The DAC arrived at **−23 dB**, with WirePlumber's device route holding
`channelVolumes [0.064, 0.064]` while the node read `1.0`. Setting the sink to
1.00 put the ALSA control at exactly 30/30 = 0.00 dB.

#### The node reads 1.0 while the route does the attenuating

This is the trap on both cards and it is worth stating on its own, because the
node is what everything reports and the route is what is actually applied:

```
NODE  volume=1.0  channelVolumes=[1.0, 1.0]      <- looks correct
ROUTE 'analog-output' channelVolumes=[0.064, …]  <- what you actually hear
```

`50-aux-softmixer.conf` sets `api.alsa.soft-mixer` on the bcm2835 card **only**,
and it needs both a device match and a node match, on *different keys* -
`alsa.card_name` is not yet populated on the device object when device rules are
evaluated, only `api.alsa.card.name` is. Match the device on the wrong one and
it silently does not match while the node prop still reads `True`.

That rule is deliberately left in place. The USB DAC does not need it, since its
mixer cannot boost, and the rule simply does not match while the DAC is plugged
in. Unplug the DAC and the built-in jack is protected again with no config
change.

`device.restore-routes = false` looks like the fix and is not: with route restore
off, WirePlumber applies `device.routes.default-sink-volume` instead, which
defaults to exactly the 0.40 you were trying to escape.

### The Zero had been playing 5.4 dB quiet all along

Worth recording because nothing looked wrong. With both rooms up, the Zero's
HDMI output measured 0.2414 peak against a 0.4500 source — ×0.5365 — while
hifi2's aux measured exactly as predicted. Every obvious volume read unity: sink
1.00, `channelVolumes [1.0, 1.0]`, device route [1.0, 1.0], ALSA `PCM` 255/255
at 0.00 dB. Playing straight to the sink with Snapcast out of the picture gave
the identical figure, so it was never Snapcast.

It was WirePlumber's saved stream state:

```
Output/Audio:media.role:Music={"channelVolumes":[0.536371, 0.536371], ...}
```

Keyed on the **`Music` role**, not on an application — which is why `pw-play`
and `snapclient`, two unrelated programs, were attenuated by precisely the same
factor. It predates this branch entirely: `main`'s stated design is "sink at
1.00, no digital attenuation, let the receiver do the work", and a stale
per-role stream volume had been quietly defeating it. Reset to 1.0, the Zero
now reproduces the source exactly.

The lesson generalises: on this stack a volume you can see being 1.00 is not
evidence that the signal is untouched. Measure the output.

### Mono, on the right channel only

hifi2 has one speaker, wired to the right side of the jack.
`snapcast/server/20-mono-right.conf` publishes a sink with a single `MONO`
channel and loops it into the hardware sink positioned at `FR`.

- **Both source channels survive**, downmixed `0.5·FL + 0.5·FR`. Sending stereo
  and simply not connecting the left plug would throw away everything panned
  left. The coefficients sum to 1.0, so a centred mix cannot clip.
- **`stream.dont-remix = true` is what makes it one channel.** Without it
  PipeWire upmixes the single channel back to both `FL` and `FR`.
- **Targeting by node name survives a default-sink change** — plug a monitor
  into hifi2 and WirePlumber may move the default; playback does not care.

### The USB DAC is 48 kHz only

`/proc/asound/card3/stream0` reports a **single** supported rate, so the
"nothing resamples anywhere" property this repo used to have no longer holds for
the aux room.

The graph nevertheless stays pinned at 44100, which is deliberate. That keeps the
Spotify source, the FIFO feeding snapserver and the entire HDMI room native, and
confines the conversion to the one device that cannot avoid it - one resample,
inside the ALSA node, on a Pi 4 with cores to spare. Moving the graph to 48000
would resample for *every* room in order to spare the one endpoint that has to
convert regardless. Check both ends:

```bash
pw-metadata -n settings | grep clock.rate      # graph:  44100
cat /proc/asound/card3/pcm0p/sub0/hw_params    # device: 48000
```

### 44.1 kHz and FLAC on the wire

Spotify streams at 44.1 kHz. PipeWire defaults to 48 kHz and snapserver defaults
to 48000:16:2; both are pinned to 44100:16:2 so nothing resamples between
Soloist and either speaker. The bcm2835 device takes 44.1 kHz directly
(`hw_params` reports `rate: 44100`, no plug layer), and so does the vc4hdmi.

Snapcast's transport codec is FLAC — lossless, and roughly half the wifi traffic
of PCM's 1.4 Mbit/s per client, which matters for a Zero 2 W on wifi.

Two honest limits on the word *lossless*: Spotify's own stream quality is an
account setting, not something this repo controls; and hifi2's analog stage is a
firmware-driven PWM DAC whose behaviour is neither visible nor controllable from
Linux. What is guaranteed here is that nothing between Soloist and the DAC
resamples or attenuates.

### Soloist reaches snapserver through a pipe-tunnel sink

The usual recipe for feeding snapserver from PipeWire is a null sink plus a
`pw-record`/`parec` process capturing its monitor. `libpipewire-module-pipe-tunnel`
does it as one object: a sink that writes raw PCM straight into the FIFO. No
capture process to supervise, and no WAV header — which snapserver's pipe source
would otherwise play as a click at the start of the stream.

The FIFO is pre-created by `tmpfiles.d` as `filip:_snapserver 0640` rather than
by either side, so its ownership does not depend on which starts first.

### Both snapclients are user units

The packaged `snapclient.service` runs as `_snapclient`, which cannot reach
`filip`'s PipeWire socket in `XDG_RUNTIME_DIR` — and on hifi2 PipeWire is what
puts the audio on one channel. The packaged unit is disabled on both boxes and
replaced with a user unit; `loginctl enable-linger` is what lets those run
without a login session.

### The screen and CEC read snapserver

`meta_soloist.py` is a snapserver stream plugin: Soloist's WebSocket in,
snapserver's plugin protocol out, so every client can ask the server what is
playing. Without it a `pipe://` source carries raw PCM and no metadata at all.

It is **metadata only**. Soloist's WebSocket command envelope is undocumented,
and guessing at it risks sending the wrong thing to a live session, so
`canControl` is false and control requests get a proper JSON-RPC *method not
found* rather than being silently accepted. Play/pause from snapweb is therefore
not wired up; use Spotify.

Both daemons on the Zero send `Server.GetStatus` on connect, because snapserver
says nothing until state changes — without it the screen would sit on *Ready*
until the next track and CEC would miss a session already in progress. They then
follow `Stream.OnProperties` and `Stream.OnUpdate`. The stream going **idle** is
a state Soloist had no equivalent of, and it clears the screen rather than
leaving the last track up forever.

`main`'s "adopt the first state silently" rule survives unchanged and matters
for the same reason: the `GetStatus` reply may describe a session that has been
sitting paused for hours, and acting on it would claim active source — switching
the receiver's input and waking the TV — on every restart and every boot.

## Install

```bash
git clone -b aux <this-repo> ~/soloist-hdmi
cd ~/soloist-hdmi
./install.sh server      # on hifi2
./install.sh client      # on hifi
```

On the server, put your API key in `~/.config/soloist-hdmi/env`, then
`systemctl --user restart soloist.service`. Pair once by picking **Hi-Fi
System** in Spotify. For the Hub, pair it to **Hi-Fi System** over Bluetooth in
the Google Home app under the device's audio settings.

## Operating it

```bash
# server
systemctl status snapserver
systemctl --user status soloist snapclient-aux bt-audio-route
scripts/find-audio-node.sh                           # list sinks

# client
systemctl --user status snapclient-hdmi snapcast-display snapcast-cec
```

The web UI is at **http://192.168.0.134:1780** — clients, groups, volumes and
which stream each group is playing. Both speakers live in one group,
*Whole house*, so one volume control moves the house.

Clients are pinned with `--hostID` (`hifi2-aux`, `hifi-hdmi`) rather than
defaulting to the MAC address, so they keep their identity and settings across
reinstalls instead of accumulating as stale entries.

## Troubleshooting

**No sound anywhere.** Check the stream is not idle and that Soloist is actually
linked to it — Soloist does not reconnect to PipeWire on its own, so restarting
PipeWire silently orphans it:

```bash
pw-link -l | grep snapcast_spotify        # should show spotify -> snapcast_spotify
systemctl --user restart soloist.service  # if not
```

**One room silent.** `snapclient` logs `No chunks available` when the stream is
idle, which is normal. If it says that while the other room plays, check the
group assignment in the web UI.

**A room is quieter than the other.** Read *The Zero had been playing 5.4 dB
quiet* above, then check the saved stream state on the quiet box:

```bash
grep media.role:Music ~/.local/state/wireplumber/stream-properties
```

**Verifying the aux output: capture the sink MONITOR, not "the device".** This
matters more than it sounds and produced several confidently wrong readings here
before it was spotted. The USB DAC is a combo device - it has a microphone as
well as playback - so:

```bash
pw-record --target <sink> out.wav          # WRONG: records the MICROPHONE
```

silently attaches to the card's capture side instead of the sink's monitor. It
succeeds, writes a plausible file, and shows both channels identical at a low
noise floor with none of your signal in it - which reads exactly like "the
routing is broken" when the routing is fine. Ask for the monitor explicitly:

```bash
pw-record -P '{ stream.capture.sink=true }' --target <sink> \
          --channels 2 --rate 48000 --format s16 out.wav
```

The tell that you are looking at a bogus capture rather than a real fault is
**byte-identical channels**: a real stereo capture of a mono-to-right setup has
one channel at digital zero, not both channels equal. A correct capture here
reads `LEFT peak 0.000 / -inf` and `RIGHT peak 0.450, both tones at -12.96 dB`.

**Sound distorted on the aux box.** `amixer -c 0 sget PCM` showing
`[100%] [4.00dB]` means the soft-mixer rule is not taking effect. Re-pin with
`amixer -c 0 sset PCM 0dB && sudo alsactl store`.

**The Hub will not pair.** The adapter must be unblocked and discoverable —
`rfkill list bluetooth` must show *Soft blocked: no*, which it is not by default
on a fresh image.

**Screen blank or stale.** `systemctl --user status snapcast-display`, then
check it reached the server: it logs `connecting to ws://…:1780/jsonrpc`.

**HDMI cuts out for a few seconds, repeatedly.** Not the buffer, not the
receiver, and not the Zero struggling - load sits at 0.06 and the local aux
client never drops in the same window. The client is tearing down its whole
session and reconnecting, and every reconnect stops and restarts the player.
That gap is what you hear. Count them:

```bash
journalctl -u snapserver --since -30min | grep -c 'onDisconnect: hifi-hdmi'
```

The client log names the trigger:

```
[Error] (Controller) Time sync request failed: Connection timed out
```

That timeout is **2 seconds**, and it is not configurable. So any stall on the
link longer than two seconds costs an audible dropout, no matter how large the
audio buffer is - a bigger buffer does not help here and arguably hurts, since
it takes longer to refill after the reconnect.

The cause is the wifi link itself. Measure it rather than guessing:

```bash
ping -c 120 -i 0.5 <the Zero>                       # loss and max rtt
ssh <the Zero> /usr/sbin/iw dev wlan0 link          # rx bitrate is the tell
ssh <the Zero> /usr/sbin/iw dev wlan0 station dump  # tx failed, climbing
```

A healthy link answers in single-digit milliseconds with no loss. This one
measured 3.3 % loss, 432 ms worst case, and `tx failed` climbing about 1.6 per
second, at an **rx bitrate of 6.5 Mbit/s**.

The asymmetry between the two boxes is the whole story, and it is structural:

| | hifi2 | hifi (Zero 2 W) |
|---|---|---|
| SSID | `McDonalds Guest` (5 GHz) | `McDonalds Guest Slow` (2.4 GHz) |
| rx bitrate | 433 Mbit/s | 6.5 Mbit/s |

**The Pi Zero 2 W has no 5 GHz radio.** It cannot join the fast SSID, and the
fast SSID has no 2.4 GHz counterpart to fall back to, so nothing on the Pi can
fix this - the lever is the radio environment, not the software. In order of
effect:

- **Wire it**, if it ever comes back. The Zero 2 W's micro-USB OTG port takes a
  USB-Ethernet adapter. This is the only change that removes 2.4 GHz from the
  picture entirely rather than making it good enough.
- **Keep the 2.4 GHz mesh on a non-overlapping channel.** This is what actually
  fixed it here, and it cost nothing. 2.4 GHz has only three non-overlapping
  channels - 1, 6 and 11 - and the mesh was on **3**, which collides with both 1
  (a neighbouring AP a few dB down) and 6. Moving it to 6, which scanned empty:

  | | channel 3 | channel 6 |
  |---|---|---|
  | reconnects | ~1.4/min | **0 in 8 min** |
  | packet loss | 3.33 % | 0.83 % |
  | max rtt | 432 ms | 180 ms |
  | rx bitrate | 6.5 Mbit/s | 19.5 Mbit/s |
  | `tx failed` | ~1.6/s | 0.025/s |

  Measured with music streaming continuously in both windows, which is worth
  saying because an idle stream sends nothing and would show zero reconnects for
  the wrong reason.
- **Move the Zero, or put a mesh node nearer it.** -65 dBm at 6.5 Mbit/s rx
  suggests distance or something absorbing, an AV cabinet included.

**Wifi power save is disabled here** (`snapcast/client-hdmi/wifi-powersave.conf`)
because `brcmfmac` enables it by default and it parks the radio between beacons.
Worth keeping on a mains-powered speaker, but be honest about it: turning it off
did **not** measurably change the dropout rate on this link, which stayed at
roughly one reconnect a minute either side of the change. It was not the cause.

**HDMI plays silently — the Pi thinks it is playing but nothing comes out.**
Almost always the receiver is *on* but showing a different input, so the audio
has nowhere to land. The tell is the ELD, which is the receiver's reply about
what it can play:

```bash
wc -c /proc/asound/card0/eld#0     # 0 bytes = nobody downstream is listening
```

Zero bytes with the connector `connected` and `hw_params` reading `RUNNING` is
exactly this. Claim the input back:

```bash
cec-ctl -d /dev/cec0 -s --to 0 --image-view-on
cec-ctl -d /dev/cec0 -s --active-source phys-addr=0x2300
```

The ELD fills in immediately if that was it. `snapcast-cec` now does this by
itself when it connects and finds the stream *already playing* — see the `first`
branch in the main loop, and note it deliberately still does **not** do it for a
stream that is merely paused, which would steal the input on every boot.

It does not fight you either: if you switch the receiver to another input
mid-track on purpose, nothing switches it back. That is the same restraint as
the idle timer's, and the cost is that this failure can recur by hand.

**CEC does nothing.** Everything in `main`'s README still applies — the physical
and logical addresses fail independently, and `cec-ctl -d /dev/cec0` must show a
real address and mask `0x0010`.
