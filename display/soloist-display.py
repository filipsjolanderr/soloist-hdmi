#!/usr/bin/env python3
"""Now-playing screen for the HDMI output.

Listens to the Soloist WebSocket API and paints album art, title, artist and
album straight to the Linux framebuffer. No X11, no Wayland, no browser - a Pi
Zero 2 W has ~400 MB of RAM and one small core, so a kiosk browser is not an
option.

The framebuffer console (fbcon) must be unbound first or it will draw the text
console over us; see scripts/fbcon.sh.
"""
import asyncio, fcntl, hashlib, io, json, logging, math, os, struct, sys
import urllib.request
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
import numpy as np
import websockets

LOG = logging.getLogger("display")

STATE_DIR = Path(os.environ.get("STATE_DIRECTORY",
                 Path.home() / ".local/state/soloist"))
CACHE_DIR = Path(os.environ.get("CACHE_DIRECTORY",
                 Path.home() / ".cache/soloist-display"))

# Spotify sets its interface in Circular (Lineto), latterly in Spotify Mix.
# Both are licensed and neither is redistributable, so this repo cannot ship
# them - but it will use them: drop a licensed copy anywhere under
# ~/.local/share/fonts and it is picked up on the next restart. Failing that
# the screen is set in Nunito Sans, which Raspberry Pi OS ships and sets its
# own desktop in. Montserrat behind it is the closer stand-in for Circular if
# you want the geometric look back, and DejaVu is the last resort, because
# install.sh guarantees it and a plain screen beats no screen.
FONT_DIRS = (Path.home() / ".local/share/fonts",
             Path("/usr/local/share/fonts"),
             Path("/usr/share/fonts"))
FONT_FAMILIES = ("spotifymixui", "spotifymix", "circularspui", "circularsp",
                 "circularstd", "circular", "nunitosans", "montserrat",
                 "inter", "dejavusans")
# Weight per role, best first. Circular calls its regular weight Book, and
# DejaVu gives that weight no name at all, hence the empty string.
ROLE_WEIGHTS = {
    "title": ("medium", "book", "regular", ""),
    "body": ("regular", "book", "light", ""),
    "label": ("semibold", "demibold", "bold", "medium"),
}

ART_FRAC = 0.64        # album art edge length as a fraction of screen height
ART_RADIUS = 0.025     # art corner radius as a fraction of its edge
LEADING = 0.42         # gap under a line, as a fraction of its size

# OLED pixel shift. Every static edge on screen - the artwork's border above
# all - is walked slowly around a small circle so it never sits on the same
# subpixels for long. The excursion is 2*SHIFT_RADIUS px over a full cycle,
# below the threshold of notice at 2 m but well past a pixel.
SHIFT_RADIUS = 8
SHIFT_STEPS = 16
SHIFT_SECONDS = 45     # a full cycle therefore takes 12 minutes

# One neutral ramp on pure black. Nothing on screen is tinted from the
# artwork: the cover is the only saturated thing in the frame, and a second
# colour pulled out of it only ever competes with it.
BLACK = (0, 0, 0)
TEXT = (244, 244, 244)         # title
TEXT_MUTED = (150, 150, 150)   # artist
TEXT_FAINT = (100, 100, 100)   # album, status
PLACEHOLDER = (20, 20, 20)     # stands in for missing artwork


# --------------------------------------------------------------------------
# Fonts
# --------------------------------------------------------------------------
def _squash(s):
    """Case and punctuation dropped. This is what lets one table match
    "CircularStd-Book.otf", "Circular Std Book.ttf" and "SemiBold" alike."""
    return "".join(c for c in s.lower() if c.isalnum())


def _font_index():
    """Every font file on the search path, keyed by its squashed stem.

    Earlier directories win, so a font dropped in $HOME overrides the
    packaged one.
    """
    idx = {}
    for d in FONT_DIRS:
        if not d.is_dir():
            continue
        for f in sorted(d.rglob("*")):
            if f.suffix.lower() in (".otf", ".ttf"):
                idx.setdefault(_squash(f.stem), f)
    return idx


def _instances(path):
    """The named instances of a variable font, keyed by squashed name.

    A static family spells each weight in a filename. A variable one keeps
    them all in one file - "NunitoSans-VariableFont_YTLC,opsz,wdth,wght.ttf" -
    and names them inside, so opening it is the only way to see what it
    holds. A static file raises here and simply has none. Italics call
    themselves "Medium Italic" and so on, which is why they never answer to a
    weight and the roman file always wins.
    """
    try:
        names = ImageFont.truetype(str(path), 10).get_variation_names()
    except (OSError, AttributeError):
        return {}
    return {_squash(n): n for n in (b.decode("utf-8", "replace") for b in names)}


def _family_faces(family, idx):
    """Every weight one family offers, as (file, instance) pairs.

    Only files whose own name starts with the family are opened, so the
    variable-font probe costs a load or two rather than a sweep of the entire
    font path - 1.5 s of it, on this board.
    """
    faces = {}
    for key, path in idx.items():
        if not key.startswith(family):
            continue
        faces.setdefault(key[len(family):], (path, None))
        for weight, instance in _instances(path).items():
            faces.setdefault(weight, (path, instance))
    return faces


def resolve_fonts():
    """Pick one family for the whole screen, and a face for each role.

    The first family with any face at all wins outright. Filling a missing
    weight from the next family down would put two designs on one screen,
    which reads as a bug rather than as a fallback - so a family that is
    present but partial repeats its own faces instead.
    """
    idx = _font_index()
    for family in FONT_FAMILIES:
        available = _family_faces(family, idx)
        faces = {}
        for role, weights in ROLE_WEIGHTS.items():
            for w in weights:
                if w in available:
                    faces[role] = available[w]
                    break
        if not faces:
            continue
        spare = faces.get("body") or next(iter(faces.values()))
        return family, {r: faces.get(r, spare) for r in ROLE_WEIGHTS}
    raise RuntimeError(f"no usable font found under {', '.join(map(str, FONT_DIRS))}"
                       " - install fonts-nunito-sans")


def load_face(face, size):
    """One face at one size. A variable font opens at its default instance -
    ExtraLight, in Nunito Sans - so the weight has to be named to take."""
    path, instance = face
    f = ImageFont.truetype(str(path), size)
    if instance:
        f.set_variation_by_name(instance)
    return f


FONT_FAMILY, _FACES = resolve_fonts()
F_TITLE, F_BODY, F_LABEL = _FACES["title"], _FACES["body"], _FACES["label"]

# --------------------------------------------------------------------------
# Framebuffer
# --------------------------------------------------------------------------
class Framebuffer:
    """RGB565 framebuffer."""

    def __init__(self, path="/dev/fb0"):
        self.path = path
        with open(path, "rb") as f:
            v = fcntl.ioctl(f, 0x4600, bytes(160))   # FBIOGET_VSCREENINFO
            fs = fcntl.ioctl(f, 0x4602, bytes(80))   # FBIOGET_FSCREENINFO
        vals = struct.unpack("20I", v[:80])
        self.width, self.height, self.bpp = vals[0], vals[1], vals[6]
        red, green, blue = vals[8:11], vals[11:14], vals[14:17]
        self.line_length = struct.unpack("I", fs[48:52])[0]

        if self.bpp != 16 or (red[0], red[1], green[0], green[1], blue[0], blue[1]) \
                != (11, 5, 5, 6, 0, 5):
            raise RuntimeError(
                f"expected RGB565, got bpp={self.bpp} r={red} g={green} b={blue}")
        if self.line_length != self.width * 2:
            raise RuntimeError(
                f"padded scanlines unsupported: line_length={self.line_length}")
        LOG.info("framebuffer %dx%d RGB565", self.width, self.height)

    # 4x4 Bayer matrix. Truncating 8-bit colour to RGB565 posterises smooth
    # gradients into visible rings on a large TV; dithering the low bits first
    # trades that for imperceptible noise. Black is unaffected - the offset
    # cannot lift 0 above the first quantisation step - so it stays at 0,0,0.
    _BAYER = (np.array([[0, 8, 2, 10], [12, 4, 14, 6],
                        [3, 11, 1, 9], [15, 7, 13, 5]], dtype=np.float32) + 0.5) / 16.0

    @classmethod
    def _to_565(cls, img: Image.Image) -> bytes:
        a = np.asarray(img.convert("RGB"), dtype=np.int16)
        h, w = a.shape[:2]
        tile = np.tile(cls._BAYER, (h // 4 + 1, w // 4 + 1))[:h, :w]
        # red/blue keep 5 bits (8 levels of error), green keeps 6 (4 levels)
        a[:, :, 0] = np.clip(a[:, :, 0] + (tile * 8 - 4), 0, 255)
        a[:, :, 1] = np.clip(a[:, :, 1] + (tile * 4 - 2), 0, 255)
        a[:, :, 2] = np.clip(a[:, :, 2] + (tile * 8 - 4), 0, 255)
        a = a.astype(np.uint16)
        packed = ((a[:, :, 0] >> 3) << 11) | ((a[:, :, 1] >> 2) << 5) | (a[:, :, 2] >> 3)
        return packed.astype("<u2").tobytes()

    def blit(self, img: Image.Image):
        with open(self.path, "wb") as f:
            f.write(self._to_565(img))


# --------------------------------------------------------------------------
# Track state
# --------------------------------------------------------------------------
class Track:
    def __init__(self):
        self.title = self.artist = self.album = ""
        self.cover_url = None
        self.status = "stopped"
        self.device_name = ""

    @property
    def key(self):
        """Identity for deciding whether a repaint is needed."""
        return (self.title, self.artist, self.album, self.cover_url, self.status)

    def update(self, msg):
        if msg.get("type") == "auth_state":
            self.device_name = msg.get("device_name", "") or self.device_name
            return
        if msg.get("type") != "playback_state":
            return
        self.status = msg.get("status", "stopped")
        item = msg.get("item") or {}
        dec = item.get("decorations") or {}
        self.title = (dec.get("identity") or {}).get("name", "") or ""

        creators = dec.get("creators") or []
        names = [((c.get("entity") or {}).get("decorations") or {})
                 .get("identity", {}).get("name", "") for c in creators]
        self.artist = ", ".join(n for n in names if n)

        parent = (dec.get("parent") or {}).get("entity") or {}
        self.album = ((parent.get("decorations") or {})
                      .get("identity") or {}).get("name", "") or ""

        covers = (dec.get("visual_identity") or {}).get("cover") or []
        by_size = {c.get("size"): c.get("url") for c in covers}
        self.cover_url = (by_size.get("large") or by_size.get("xlarge")
                          or by_size.get("default") or by_size.get("small"))


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------
def fetch_cover(url: str) -> Image.Image | None:
    if not url:
        return None
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / (hashlib.sha1(url.encode()).hexdigest() + ".img")
    try:
        if not path.exists():
            req = urllib.request.Request(url, headers={"User-Agent": "soloist-display"})
            with urllib.request.urlopen(req, timeout=10) as r:
                path.write_bytes(r.read())
        return Image.open(io.BytesIO(path.read_bytes())).convert("RGB")
    except Exception as e:
        LOG.warning("cover fetch failed (%s): %s", url, e)
        path.unlink(missing_ok=True)
        return None


def rounded_mask(size: int, radius: int, supersample: int = 4) -> Image.Image:
    """Antialiased rounded-square alpha mask for the artwork.

    PIL fills rounded rectangles with hard pixels, and stepped corners are
    obvious on a TV. Drawing 4x oversized and shrinking with LANCZOS smooths
    them; the mask only depends on the art size, so it is built once.
    """
    s = size * supersample
    mask = Image.new("L", (s, s), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, s - 1, s - 1), radius=radius * supersample, fill=255)
    return mask.resize((size, size), Image.LANCZOS)


def text_width(d, text, font, tracking=0.0):
    if not tracking:
        return d.textlength(text, font=font)
    return (sum(d.textlength(c, font=font) for c in text)
            + tracking * max(0, len(text) - 1))


def draw_text(d, xy, text, font, fill, tracking=0.0):
    """Draw a line, letter by letter when tracked - PIL has no tracking."""
    if not tracking:
        d.text(xy, text, font=font, fill=fill)
        return
    x, y = xy
    for ch in text:
        d.text((x, y), ch, font=font, fill=fill)
        x += d.textlength(ch, font=font) + tracking


def fit_font(d, text, face, size, max_w, min_size, tracking=0.0):
    """Shrink until it fits, then hard-truncate with an ellipsis."""
    while size > min_size:
        f = load_face(face, size)
        if text_width(d, text, f, tracking) <= max_w:
            return f, text
        size -= 2
    # The loop above never tests min_size itself, and the status label asks
    # for a size that is already its own floor - without this it would come
    # out as "PAUSED…", ellipsed with 500 px of room to spare.
    f = load_face(face, min_size)
    if text_width(d, text, f, tracking) <= max_w:
        return f, text
    while text and text_width(d, text + "…", f, tracking) > max_w:
        text = text[:-1]
    return f, (text + "…" if text else "")


class Renderer:
    def __init__(self, w, h):
        self.w, self.h = w, h
        self.margin = int(h * 0.10)
        self.art = int(h * ART_FRAC)
        self.art_x = self.margin
        self.art_y = (h - self.art) // 2
        self.art_bottom = self.art_y + self.art
        self.text_x = self.art_x + self.art + int(w * 0.05)
        self.text_w = w - self.text_x - self.margin
        self.art_mask = rounded_mask(self.art, max(8, int(self.art * ART_RADIUS)))
        self.set_cover(None)

    # -- text -------------------------------------------------------------
    def _layout(self, d, rows, max_w):
        """Fit each line to max_w and stack it. Returns the laid-out lines,
        their y offsets, and the ink bounds of the stack as a whole."""
        laid, offsets, y = [], [], 0
        for face, size, text, fill, tracking in rows:
            font, text = fit_font(d, text, face, size, max_w,
                                  max(15, int(size * 0.55)), tracking)
            laid.append((font, text, fill, tracking))
            offsets.append(y)
            y += font.size + int(font.size * LEADING)

        boxes = [d.textbbox((0, off), t, font=f)
                 for (f, t, _, _), off in zip(laid, offsets) if t]
        top = min(b[1] for b in boxes) if boxes else 0
        bottom = max(b[3] for b in boxes) if boxes else 0
        return laid, offsets, top, bottom

    def _draw_lines(self, d, laid, offsets, x, y, centre_in=None):
        for (font, text, fill, tracking), off in zip(laid, offsets):
            tx = x
            if centre_in is not None:
                tx = x + (centre_in - text_width(d, text, font, tracking)) / 2
            draw_text(d, (tx, y + off), text, font, fill, tracking)

    # -- full frame -------------------------------------------------------
    def render(self, track: Track) -> Image.Image:
        img = Image.new("RGB", (self.w, self.h), BLACK)
        d = ImageDraw.Draw(img)

        # Idle: nothing but a line of text on black. This screen is up for
        # hours between listening sessions, and a static panel is the one
        # thing not to leave sitting on an OLED.
        if not track.title:
            rows = [(F_TITLE, 40, "Ready", TEXT, 0),
                    (F_BODY, 22,
                     f"Select “{track.device_name or 'this device'}” in Spotify",
                     TEXT_FAINT, 0)]
            laid, offsets, top, bottom = self._layout(d, rows, self.w - 2 * self.margin)
            self._draw_lines(d, laid, offsets, self.margin,
                             (self.h - (bottom - top)) // 2 - top,
                             centre_in=self.w - 2 * self.margin)
            return img

        img.paste(self.artwork, (self.art_x, self.art_y), self.art_mask)

        rows = []
        if track.status != "playing":
            rows.append((F_LABEL, 15, track.status.upper(), TEXT_FAINT, 3.5))
        rows.append((F_TITLE, 50, track.title, TEXT, 0))
        rows.append((F_BODY, 30, track.artist, TEXT_MUTED, 0))
        rows.append((F_BODY, 23, track.album, TEXT_FAINT, 0))

        laid, offsets, _, _ = self._layout(d, [r for r in rows if r[2]], self.text_w)
        # Sit the block on the artwork's bottom edge, aligned on the last
        # line's baseline rather than its ink: baselines do not move when a
        # title happens to have no descender, so the block stays put as
        # tracks change.
        ascent = laid[-1][0].getmetrics()[0]
        self._draw_lines(d, laid, offsets, self.text_x,
                         self.art_bottom - offsets[-1] - ascent)
        return img

    def set_cover(self, cover: Image.Image | None):
        self.artwork = (cover.resize((self.art, self.art), Image.LANCZOS)
                        if cover is not None
                        else Image.new("RGB", (self.art, self.art), PLACEHOLDER))

    def shifted(self, base: Image.Image, step: int) -> Image.Image:
        """The frame, walked one step around the pixel-shift circle."""
        a = 2 * math.pi * (step % SHIFT_STEPS) / SHIFT_STEPS
        img = Image.new("RGB", (self.w, self.h), BLACK)
        img.paste(base, (round(SHIFT_RADIUS * math.cos(a)),
                         round(SHIFT_RADIUS * math.sin(a))))
        return img


# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------
def ws_url():
    addr_f, port_f = STATE_DIR / "ws.addr", STATE_DIR / "ws.port"
    addr = addr_f.read_text().strip() if addr_f.exists() else "127.0.0.1"
    port = port_f.read_text().strip() if port_f.exists() else "3678"
    return f"ws://{addr}:{port}"


async def run():
    LOG.info("fonts: %s (%s)", FONT_FAMILY,
             ", ".join(f"{r} {i or p.stem}" for r, (p, i) in _FACES.items()))
    fb = Framebuffer()
    rend = Renderer(fb.width, fb.height)
    track = Track()
    base = None
    shift = 0
    last_key = None
    loaded_url = None

    def paint():
        fb.blit(rend.shifted(base, shift))

    async def repaint_full():
        nonlocal base
        base = rend.render(track)
        paint()

    await repaint_full()

    async def pixel_shift():
        nonlocal shift
        while True:
            await asyncio.sleep(SHIFT_SECONDS)
            shift += 1
            try:
                paint()
            except Exception as e:
                LOG.warning("pixel shift failed: %s", e)

    asyncio.create_task(pixel_shift())

    backoff = 1
    while True:
        try:
            url = ws_url()
            LOG.info("connecting to %s", url)
            async with websockets.connect(url, ping_interval=20) as ws:
                backoff = 1
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    track.update(msg)
                    if track.key != last_key:
                        last_key = track.key
                        # keyed on what actually loaded, so a failed fetch is
                        # retried and a track with no artwork falls back to the
                        # placeholder instead of keeping the previous cover
                        if track.cover_url != loaded_url:
                            new = await asyncio.to_thread(fetch_cover, track.cover_url)
                            if new is not None or track.cover_url is None:
                                rend.set_cover(new)
                                loaded_url = track.cover_url
                        await repaint_full()
        except Exception as e:
            LOG.warning("websocket error: %s (retry in %ds)", e, backoff)
            track.status = "stopped"
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s",
                        stream=sys.stdout)
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
