#!/usr/bin/env python3
"""Now-playing screen for the HDMI output.

Listens to the Soloist WebSocket API and paints album art, title, artist and
album straight to the Linux framebuffer. No X11, no Wayland, no browser - a Pi
Zero 2 W has ~400 MB of RAM and one small core, so a kiosk browser is not an
option.

The composition is a centred column: cover, title under it, then artist and
album on their own lines under that. Anything that is not playing says so
with a glyph on the cover - a pause bar, or a spinner while it buffers -
rather than with a word: a caption set in caps is one more thing to read from
the sofa, and a shape lands before you focus on it.

The framebuffer console (fbcon) must be unbound first or it will draw the text
console over us; see scripts/fbcon.sh.
"""
import asyncio, fcntl, hashlib, io, json, logging, math, os, struct, sys, time
import urllib.request
from functools import lru_cache
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
}

# Layout. Every measure is a fraction of the screen height, so 720p and 1080p
# get the same composition instead of the same pixel sizes - the screen is
# read from ~2 m either way, and the panel's resolution is not the point.
ART_FRAC = 0.56        # album art edge length
ART_RADIUS = 0.025     # art corner radius, as a fraction of its edge
TITLE_FRAC = 0.058     # title size
ARTIST_FRAC = 0.032    # artist size
ALBUM_FRAC = 0.029     # album size - a step down again, under the artist
GAP_FRAC = 0.062       # art bottom to the top of the title's capitals
TITLE_LEAD = 1.12      # leading inside a two-line title, in title sizes
LINE_STEP = 1.02       # last title baseline to artist baseline, in title sizes
META_LEAD = 1.50       # artist baseline to album baseline, in artist sizes
TEXT_WIDTH = 1.9       # longest line, in cover widths
ICON_FRAC = 0.16       # status glyph, as a fraction of the cover
DIM = 0.38             # cover brightness while not playing

# OLED pixel shift. Every static edge on screen - the artwork's border above
# all - is walked slowly around a small circle so it never sits on the same
# subpixels for long. The excursion is 2*SHIFT_RADIUS px over a full cycle,
# below the threshold of notice at 2 m but well past a pixel.
SHIFT_RADIUS = 8
SHIFT_STEPS = 16
SHIFT_SECONDS = 45     # a full cycle therefore takes 12 minutes

# The buffering spinner. Its 16 phases are drawn once each and then reused, so
# a revolution costs one paste and one write of the ~60 rows it covers.
SPIN_STEPS = 16
SPIN_FPS = 12
BUSY = ("buffering", "loading")

# Album art cache. Covers are ~50 kB each and the directory would otherwise
# grow for the life of the SD card.
COVER_MAX_BYTES = 4 << 20
COVER_KEEP = 300

# How long the last frame stands after the daemon goes away. A soloist
# restart is a two-second blip and blanking the screen for it looks worse
# than holding still; a daemon that stays down should not keep claiming
# something is playing. Retries stay frequent rather than backing off into
# the minutes - the reconnect is a localhost socket, and the cost of a long
# backoff is a screen that stays wrong for half a minute after the daemon is
# back up.
DISCONNECT_GRACE = 20
RETRY_MAX = 5

# One neutral ramp on pure black. Nothing on screen is tinted from the
# artwork: the cover is the only saturated thing in the frame, and a second
# colour pulled out of it only ever competes with it.
BLACK = (0, 0, 0)
TEXT = (244, 244, 244)         # title, status glyph
TEXT_MUTED = (150, 150, 150)   # artist
TEXT_FAINT = (100, 100, 100)   # album, idle prompt - one step further back
PLACEHOLDER = (18, 18, 18)     # stands in for missing artwork
PLACEHOLDER_INK = (54, 54, 54)  # the note drawn on it


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


@lru_cache(maxsize=64)
def load_face(face, size):
    """One face at one size, cached - fitting a line tries a dozen sizes and
    reopening the file for each one is the slowest thing on the screen.

    A variable font opens at its default instance - ExtraLight, in Nunito
    Sans - so the weight has to be named to take.
    """
    path, instance = face
    f = ImageFont.truetype(str(path), size)
    if instance:
        f.set_variation_by_name(instance)
    return f


def cap_height(font):
    """Baseline to the top of a capital.

    Lines are placed on their baselines, never on their ink: ink bounds move
    with the string, so a title that happens to have no descender would
    otherwise shove the line under it around.
    """
    return font.getmetrics()[0] - font.getbbox("H")[1]


def elide(text, font, max_w):
    """Hard-truncate with an ellipsis. The last resort, after shrinking."""
    if font.getlength(text) <= max_w:
        return text
    while text and font.getlength(text.rstrip() + "…") > max_w:
        text = text[:-1]
    return text.rstrip() + "…" if text else ""


def fit(text, face, size, max_w, floor=0.62):
    """Shrink until it fits, then elide. Returns the font and the text.

    Shrinking first is what keeps a long title whole; the floor is where
    legibility from the sofa gives out and truncation is the lesser evil.
    """
    min_size = max(14, round(size * floor))
    while size > min_size:
        f = load_face(face, size)
        if f.getlength(text) <= max_w:
            return f, text
        size -= 2
    f = load_face(face, min_size)
    return f, elide(text, f, max_w)


def wrap2(text, font, max_w):
    """Split into two lines as evenly as the words allow, or None if there is
    nothing to split. A centred title wants balance, not fill: greedy
    wrapping leaves a long line over a short one, which reads as a mistake.
    """
    words = text.split()
    if len(words) < 2:
        return None
    return min((max(font.getlength(" ".join(words[:i])),
                    font.getlength(" ".join(words[i:]))),
                " ".join(words[:i]), " ".join(words[i:]))
               for i in range(1, len(words)))


def fit_title(text, face, size, max_w):
    """The title, as large as it can be: one line, or two.

    Everything else on this screen is a caption and shrinks quietly. The
    title is the one thing worth reading from the far side of the room, so
    it gives up a tenth of its size at most before it takes a second line
    instead - a long title set small under a large cover looks like a
    caption, and a wrapped one still looks like a title.
    """
    floor = max(14, round(size * 0.62))
    for s in range(size, max(14, round(size * 0.9)) - 1, -2):
        f = load_face(face, s)
        if f.getlength(text) <= max_w:
            return f, [text]
    for s in range(size, floor - 1, -2):
        f = load_face(face, s)
        split = wrap2(text, f, max_w)
        if split and split[0] <= max_w:
            return f, list(split[1:])
        if f.getlength(text) <= max_w:      # one long unbreakable word
            return f, [text]
    f = load_face(face, floor)
    split = wrap2(text, f, max_w)
    if not split:
        return f, [elide(text, f, max_w)]
    return f, [elide(split[1], f, max_w), elide(split[2], f, max_w)]


FONT_FAMILY, _FACES = resolve_fonts()
F_TITLE, F_BODY = _FACES["title"], _FACES["body"]


# --------------------------------------------------------------------------
# Glyphs
# --------------------------------------------------------------------------
def mask(size, paint, supersample=4):
    """An antialiased alpha mask, `size` square.

    PIL fills shapes with hard pixels and stepped edges are obvious on a TV,
    so everything with a curve in it - the artwork's corners, the glyphs -
    is drawn 4x oversized and shrunk with LANCZOS. Painters work in fractions
    of the side they are handed, which is what makes one set of proportions
    serve every screen size.
    """
    s = size * supersample
    m = Image.new("L", (s, s), 0)
    paint(ImageDraw.Draw(m), s)
    return m.resize((size, size), Image.LANCZOS)


def paint_rounded(radius):
    return lambda d, s: d.rounded_rectangle((0, 0, s - 1, s - 1),
                                            radius=radius * s, fill=255)


def paint_pause(d, s):
    """Two bars, cornered like the artwork rather than squared off."""
    w, gap = 0.31 * s, 0.17 * s
    x = (s - (2 * w + gap)) / 2
    for i in range(2):
        d.rounded_rectangle((x + i * (w + gap), 0.06 * s,
                             x + i * (w + gap) + w, 0.94 * s),
                            radius=0.08 * s, fill=255)


def paint_spinner(phase, arc=290, steps=28):
    """A ring with a tail that fades out behind the head.

    Drawn as a fan of short arcs, each filled with its own alpha - PIL has no
    gradient stroke, and a ring of even weight reads as a static graphic
    rather than as something in progress. Both ends are cut square: a rounded
    cap at the head sits proud of a stroke this thick and turns into a bead
    going round the screen.
    """
    def paint(d, s):
        r, w = 0.42 * s, 0.135 * s
        box = (s / 2 - r, s / 2 - r, s / 2 + r, s / 2 + r)
        start = 360 * phase
        for i in range(steps):
            a = start + arc * i / steps
            # arcs overlap by a degree so the seams between them do not show
            d.arc(box, a, a + arc / steps + 1.5, width=round(w),
                  fill=round(255 * (0.26 + 0.74 * (i / (steps - 1)) ** 1.25)))
    return paint


def paint_note(d, s):
    """Two beamed quavers, for a track that has no artwork. A flat grey
    square reads as a failure; this reads as a decision."""
    rx, ry, stem, beam = 0.16 * s, 0.125 * s, 0.055 * s, 0.13 * s
    heads = ((0.26 * s, 0.78 * s, 0.26 * s), (0.72 * s, 0.66 * s, 0.14 * s))
    for cx, cy, top in heads:
        d.ellipse((cx - rx, cy - ry, cx + rx, cy + ry), fill=255)
        d.rectangle((cx + rx - stem, top, cx + rx, cy), fill=255)
    (lx, _, lt), (rx_, _, rt) = heads
    d.polygon([(lx + rx - stem, lt), (rx_ + rx, rt),
               (rx_ + rx, rt + beam), (lx + rx - stem, lt + beam)], fill=255)


# --------------------------------------------------------------------------
# Framebuffer
# --------------------------------------------------------------------------
# 4x4 Bayer matrix. Truncating 8-bit colour to RGB565 posterises smooth
# gradients into visible rings on a large TV; dithering the low bits first
# trades that for imperceptible noise. Black is unaffected - the offset
# cannot lift 0 above the first quantisation step - so it stays at 0,0,0.
_BAYER = np.array([[0, 8, 2, 10], [12, 4, 14, 6],
                   [3, 11, 1, 9], [15, 7, 13, 5]], dtype=np.int16)


@lru_cache(maxsize=8)
def _dither(h, w, phase):
    """The Bayer matrix tiled to h x w, starting at row `phase`.

    The phase matters: a partial write covers rows that did not start at a
    multiple of four, and a tile that ignored that would put a different
    dither pattern inside the band than around it - a visible seam on a flat
    tone.
    """
    return np.tile(np.roll(_BAYER, -phase, axis=0), (h // 4 + 1, w // 4 + 1))[:h, :w]


class Framebuffer:
    """RGB565 framebuffer, written by row range."""

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
        # Held open for the life of the process: at twelve spinner frames a
        # second, opening the device per write is the write.
        self.fd = os.open(path, os.O_RDWR)
        LOG.info("framebuffer %dx%d RGB565", self.width, self.height)

    @staticmethod
    def _to_565(img: Image.Image, y: int) -> bytes:
        a = np.asarray(img.convert("RGB"), dtype=np.int16)
        h, w = a.shape[:2]
        t = _dither(h, w, y % 4)
        # red/blue keep 5 bits (8 levels of error), green keeps 6 (4 levels)
        a[:, :, 0] += (t >> 1) - 4
        a[:, :, 1] += (t >> 2) - 2
        a[:, :, 2] += (t >> 1) - 4
        np.clip(a, 0, 255, out=a)
        a = a.astype(np.uint16)
        packed = ((a[:, :, 0] >> 3) << 11) | ((a[:, :, 1] >> 2) << 5) | (a[:, :, 2] >> 3)
        return packed.astype("<u2").tobytes()

    def blit(self, img: Image.Image, y: int = 0):
        """Write a full-width image at row y.

        Scanlines are unpadded, so any run of whole rows is one contiguous
        region of the device and a band can be written on its own. That is
        what makes the spinner affordable: sixty rows, not a thousand.
        """
        if img.width != self.width:
            raise ValueError(f"expected {self.width} px wide, got {img.width}")
        os.pwrite(self.fd, self._to_565(img, y), y * self.line_length)


# --------------------------------------------------------------------------
# Track state
# --------------------------------------------------------------------------
class Track:
    def __init__(self):
        self.device_name = ""
        self.clear()

    def clear(self):
        self.title = self.artist = self.album = ""
        self.cover_url = None
        self.status = "stopped"

    @property
    def key(self):
        """Identity for deciding whether a repaint is needed."""
        return (self.title, self.artist, self.album, self.cover_url, self.status)

    @property
    def indicator(self):
        """Playing shows nothing. Buffering spins. Everything else - paused,
        stopped, idle, a status this was never told about - is a pause bar:
        from the sofa the only distinction that matters is whether the music
        is moving."""
        if self.status == "playing":
            return None
        return "spin" if self.status in BUSY else "pause"

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
# Album art
# --------------------------------------------------------------------------
def prune_cache(keep=COVER_KEEP):
    """Drop all but the newest `keep` covers. Runs on a miss, which is once
    per unheard track."""
    try:
        files = sorted(CACHE_DIR.glob("*.img"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        for f in files[keep:]:
            f.unlink(missing_ok=True)
    except OSError as e:
        LOG.warning("cover cache prune failed: %s", e)


def fetch_cover(url: str) -> Image.Image | None:
    if not url:
        return None
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / (hashlib.sha1(url.encode()).hexdigest() + ".img")
    try:
        if path.exists():
            img = Image.open(io.BytesIO(path.read_bytes())).convert("RGB")
            path.touch()          # so pruning keeps what is actually listened to
            return img
        req = urllib.request.Request(url, headers={"User-Agent": "soloist-display"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = r.read(COVER_MAX_BYTES + 1)
        if len(data) > COVER_MAX_BYTES:
            raise ValueError(f"cover exceeds {COVER_MAX_BYTES} bytes")
        img = Image.open(io.BytesIO(data)).convert("RGB")
        # decoded before it is cached, and renamed into place after: a
        # truncated download never becomes a cache entry that fails forever
        tmp = path.with_suffix(".part")
        tmp.write_bytes(data)
        tmp.replace(path)
        prune_cache()
        return img
    except Exception as e:
        LOG.warning("cover fetch failed (%s): %s", url, e)
        path.unlink(missing_ok=True)
        return None


def square(img: Image.Image) -> Image.Image:
    """Centre-crop to a square. Spotify's covers are square already, but a
    stretched cover is the one artwork failure nobody forgives."""
    w, h = img.size
    if w == h:
        return img
    side = min(w, h)
    return img.crop(((w - side) // 2, (h - side) // 2,
                     (w + side) // 2, (h + side) // 2))


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------
class Renderer:
    """The composition, laid out once from the screen's dimensions.

        cover
        Title
        Artist
        Album

    The cover sits at a fixed place and the text hangs under it, so a title
    that needs two lines grows downwards instead of pushing the artwork
    around - nothing on this screen moves between tracks but the words.
    """

    def __init__(self, w, h):
        self.w, self.h = w, h
        self.art = round(h * ART_FRAC)
        self.art_x = (w - self.art) // 2
        self.f_title = load_face(F_TITLE, round(h * TITLE_FRAC))
        self.f_artist = load_face(F_BODY, round(h * ARTIST_FRAC))
        self.f_album = load_face(F_BODY, round(h * ALBUM_FRAC))

        cap = cap_height(self.f_title)
        gap = round(h * GAP_FRAC)
        self.lead = round(self.f_title.size * TITLE_LEAD)
        self.step = round(self.f_title.size * LINE_STEP)
        self.meta_lead = round(self.f_artist.size * META_LEAD)
        # Centred as though the title were a line and a half. A block centred
        # on one line crowds the bottom of the screen the moment a title
        # wraps to two; half a line of allowance leaves both cases balanced,
        # and sits the common one a touch above centre, where the eye expects
        # to find it anyway.
        block = (self.art + gap + cap + self.lead // 2 + self.step
                 + self.meta_lead + self.f_album.getmetrics()[1])
        self.art_y = max(round(h * 0.03), (h - block) // 2)
        self.title_base = self.art_y + self.art + gap + cap
        self.max_text = min(w - 2 * round(w * 0.05), round(self.art * TEXT_WIDTH))

        self.icon = round(self.art * ICON_FRAC)
        self.icon_x = (w - self.icon) // 2
        self.icon_y = self.art_y + (self.art - self.icon) // 2

        self.art_mask = mask(self.art, paint_rounded(ART_RADIUS))
        self.pause_mask = mask(self.icon, paint_pause)
        self._spin = {}
        self.set_cover(None)

    # -- pieces -----------------------------------------------------------
    def spinner(self, step):
        """One of SPIN_STEPS phases, drawn on first use and kept. Sixteen
        masks at icon size is ~60 kB and it turns each frame into a paste."""
        step %= SPIN_STEPS
        if step not in self._spin:
            self._spin[step] = mask(self.icon, paint_spinner(step / SPIN_STEPS))
        return self._spin[step]

    def paste_glyph(self, img, x, y, spin=None):
        """The status glyph at (x, y): the pause bar, or the spinner at one
        of its phases."""
        img.paste(TEXT, (x, y),
                  self.pause_mask if spin is None else self.spinner(spin))

    def set_cover(self, cover: Image.Image | None):
        if cover is None:
            art = Image.new("RGB", (self.art, self.art), PLACEHOLDER)
            note = round(self.art * 0.22)
            art.paste(PLACEHOLDER_INK, ((self.art - note) // 2,) * 2,
                      mask(note, paint_note))
        else:
            art = square(cover).resize((self.art, self.art), Image.LANCZOS)
        self.artwork = art
        # Held pre-dimmed rather than dimmed per frame: it is one LUT over a
        # 400 px square, and it is wanted on every paused repaint.
        self.artwork_dim = art.point(lambda v: round(v * DIM))

    def _meta(self, track):
        """The lines under the title, in order, each with its own grey.

        The album is dropped when it only repeats the title, which is what a
        single looks like coming out of Spotify and which otherwise puts the
        same words on the screen twice.
        """
        rows = []
        if track.artist:
            rows.append((track.artist, self.f_artist.size, TEXT_MUTED))
        if track.album and track.album.strip().lower() != track.title.strip().lower():
            rows.append((track.album, self.f_album.size, TEXT_FAINT))
        return rows

    # -- full frame -------------------------------------------------------
    def render(self, track: Track) -> Image.Image:
        """The frame, without the spinner - that is composited at blit time
        so its phase can advance without re-rendering anything."""
        img = Image.new("RGB", (self.w, self.h), BLACK)
        d = ImageDraw.Draw(img)

        # Idle: nothing but a line of text on black. This screen is up for
        # hours between listening sessions, and a static panel is the one
        # thing not to leave sitting on an OLED.
        if not track.title:
            f1 = load_face(F_TITLE, round(self.h * 0.055))
            f2, line = fit(f"Select “{track.device_name or 'this device'}” in Spotify",
                           F_BODY, round(self.h * 0.031), self.w - 2 * round(self.w * 0.08))
            step = round(f1.size * 1.15)
            base = (self.h - (cap_height(f1) + step)) // 2 + cap_height(f1)
            d.text((self.w / 2, base), "Ready", font=f1, fill=TEXT, anchor="ms")
            d.text((self.w / 2, base + step), line, font=f2, fill=TEXT_FAINT,
                   anchor="ms")
            return img

        img.paste(self.artwork if track.indicator is None else self.artwork_dim,
                  (self.art_x, self.art_y), self.art_mask)
        if track.indicator == "pause":
            self.paste_glyph(img, self.icon_x, self.icon_y)

        f, lines = fit_title(track.title, F_TITLE, self.f_title.size, self.max_text)
        lead = round(f.size * TITLE_LEAD)
        for i, line in enumerate(lines):
            d.text((self.w / 2, self.title_base + i * lead), line,
                   font=f, fill=TEXT, anchor="ms")
        # a fixed drop from the title's last baseline, so the gap under the
        # title is the same whether it wrapped or not
        y = self.title_base + (len(lines) - 1) * lead + self.step
        for text, size, fill in self._meta(track):
            mf, line = fit(text, F_BODY, size, self.max_text, floor=0.78)
            d.text((self.w / 2, y), line, font=mf, fill=fill, anchor="ms")
            y += self.meta_lead
        return img


class Screen:
    """What is actually on the panel: the rendered frame, the pixel-shift
    offset it is drawn at, and the spinner's phase."""

    def __init__(self, fb: Framebuffer, rend: Renderer):
        self.fb, self.rend = fb, rend
        self.base = rend.render(Track())
        self.shift = 0
        self.spin = 0
        self.busy = False

    @property
    def offset(self):
        a = 2 * math.pi * (self.shift % SHIFT_STEPS) / SHIFT_STEPS
        return (round(SHIFT_RADIUS * math.cos(a)), round(SHIFT_RADIUS * math.sin(a)))

    def render(self, track: Track):
        self.busy = track.indicator == "spin"
        self.base = self.rend.render(track)
        self.flush()

    def flush(self):
        """The whole screen: the frame walked to its shift position, with the
        spinner over it if there is one."""
        dx, dy = self.offset
        img = Image.new("RGB", (self.rend.w, self.rend.h), BLACK)
        img.paste(self.base, (dx, dy))
        if self.busy:
            self.rend.paste_glyph(img, self.rend.icon_x + dx,
                                  self.rend.icon_y + dy, self.spin)
        self.fb.blit(img)

    def tick(self):
        """Advance the spinner, and write only the rows it covers - one band
        of the cover rather than a megapixel and a half."""
        self.spin += 1
        dx, dy = self.offset
        r = self.rend
        band = Image.new("RGB", (r.w, r.icon), BLACK)
        band.paste(self.base.crop((0, r.icon_y, r.w, r.icon_y + r.icon)), (dx, 0))
        r.paste_glyph(band, r.icon_x + dx, 0, self.spin)
        self.fb.blit(band, r.icon_y + dy)


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
    screen = Screen(fb, Renderer(fb.width, fb.height))
    track = Track()
    spinning = asyncio.Event()
    last_key = None
    loaded_url = None

    def repaint():
        """Nothing here is worth dying for: a frame that fails to paint is
        one stale frame, and the next track change tries again."""
        try:
            screen.render(track)
        except Exception as e:
            LOG.warning("repaint failed: %s", e)
        if screen.busy:
            spinning.set()

    repaint()

    async def spinner():
        while True:
            await spinning.wait()
            while screen.busy:
                await asyncio.sleep(1 / SPIN_FPS)
                try:
                    screen.tick()
                except Exception as e:
                    LOG.warning("spinner failed: %s", e)
                    break
            spinning.clear()

    async def pixel_shift():
        while True:
            await asyncio.sleep(SHIFT_SECONDS)
            screen.shift += 1
            try:
                screen.flush()
            except Exception as e:
                LOG.warning("pixel shift failed: %s", e)

    asyncio.create_task(spinner())
    asyncio.create_task(pixel_shift())

    backoff = 1
    lost = None
    while True:
        try:
            url = ws_url()
            if lost is None:
                LOG.info("connecting to %s", url)
            async with websockets.connect(url, ping_interval=20) as ws:
                async for raw in ws:
                    # only a message clears the outage: a daemon that accepts
                    # a connection and drops it is not a daemon that is back,
                    # and resetting on connect alone would reconnect to it
                    # flat out, once a second, forever
                    if lost is not None:
                        LOG.info("reconnected after %.0fs", time.monotonic() - lost)
                        lost = None
                    backoff = 1
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
                                screen.rend.set_cover(new)
                                loaded_url = track.cover_url
                        repaint()
            # A clean close is a disconnection too, and websockets ends the
            # iterator rather than raising for one. Take the same path.
            raise ConnectionError("daemon closed the connection")
        except Exception as e:
            # one line per outage, not one per attempt: this loop runs for
            # months and an unreachable daemon should not fill the journal
            if lost is None:
                lost = time.monotonic()
                LOG.warning("websocket error: %s (retrying every %ds)", e, RETRY_MAX)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, RETRY_MAX)
            # Hold the last frame through a daemon restart; drop it if the
            # daemon stays away, rather than leaving a track on screen that
            # nothing is playing.
            if track.title and time.monotonic() - lost > DISCONNECT_GRACE:
                LOG.info("daemon gone %.0fs, clearing the screen",
                         time.monotonic() - lost)
                track.clear()
                last_key = None
                repaint()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s",
                        stream=sys.stdout)
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
