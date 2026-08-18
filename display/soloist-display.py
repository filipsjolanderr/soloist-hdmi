#!/usr/bin/env python3
"""Now-playing screen for the HDMI output.

Listens to the Soloist WebSocket API and paints album art, title and artist
straight to the Linux framebuffer. No X11, no Wayland, no browser - a Pi Zero
2 W has ~400 MB of RAM and one small core, so a kiosk browser is not an option.

The framebuffer console (fbcon) must be unbound first or it will draw the text
console over us; see scripts/fbcon.sh.
"""
import asyncio, fcntl, hashlib, io, json, logging, os, struct, sys, time
import urllib.request
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageFilter
import numpy as np
import websockets

LOG = logging.getLogger("display")

STATE_DIR = Path(os.environ.get("STATE_DIRECTORY",
                 Path.home() / ".local/state/soloist"))
CACHE_DIR = Path(os.environ.get("CACHE_DIRECTORY",
                 Path.home() / ".cache/soloist-display"))
FONT_DIR = Path("/usr/share/fonts/truetype/dejavu")

BG_DIM = 0.34          # how far the blurred backdrop is darkened
ART_FRAC = 0.63        # album art edge length as a fraction of screen height
TICK_SECONDS = 1.0     # progress bar refresh


# --------------------------------------------------------------------------
# Framebuffer
# --------------------------------------------------------------------------
class Framebuffer:
    """RGB565 framebuffer. Supports writing a full frame or a strip of rows."""

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
    # trades that for imperceptible noise.
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

    def blit_rows(self, img_strip: Image.Image, y: int):
        """Write only rows [y, y+strip.height) - avoids repainting 1.8 MB/s."""
        with open(self.path, "r+b") as f:
            f.seek(y * self.line_length)
            f.write(self._to_565(img_strip))


# --------------------------------------------------------------------------
# Track state
# --------------------------------------------------------------------------
class Track:
    def __init__(self):
        self.title = self.artist = self.album = ""
        self.cover_url = None
        self.duration_ms = 0
        self.position_ms = 0
        self.timestamp_ms = 0
        self.speed = 1
        self.status = "stopped"
        self.device_name = ""

    @property
    def key(self):
        """Identity for deciding whether a full repaint is needed."""
        return (self.title, self.artist, self.album, self.cover_url, self.status)

    def elapsed_ms(self):
        if self.status != "playing":
            return self.position_ms
        drift = (time.time() * 1000) - self.timestamp_ms
        return min(self.position_ms + drift * self.speed, self.duration_ms or 1e12)

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

        # NB: playback sits under decorations, not directly on item.
        self.duration_ms = (dec.get("playback") or {}).get("duration_ms", 0) or 0
        pos = msg.get("position") or {}
        self.position_ms = pos.get("position_ms", 0) or 0
        self.timestamp_ms = pos.get("timestamp_ms", time.time() * 1000)
        self.speed = pos.get("speed", 1) or 1


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


def accent_from(img: Image.Image):
    """Pick a saturated, TV-legible accent colour from the artwork."""
    small = img.resize((32, 32), Image.BILINEAR).convert("HSV")
    a = np.asarray(small).reshape(-1, 3).astype(int)
    # prefer saturated, mid-to-bright pixels; fall back to plain average
    good = a[(a[:, 1] > 70) & (a[:, 2] > 70)]
    if len(good) < 12:
        good = a
    h = int(np.median(good[:, 0]))
    s = min(255, int(np.median(good[:, 1])) + 60)
    v = max(170, int(np.median(good[:, 2])))
    return Image.new("HSV", (1, 1), (h, s, v)).convert("RGB").getpixel((0, 0))


def fit_font(draw, text, font_path, size, max_w, min_size=20):
    """Shrink until it fits, then hard-truncate with an ellipsis."""
    while size > min_size:
        f = ImageFont.truetype(str(font_path), size)
        if draw.textlength(text, font=f) <= max_w:
            return f, text
        size -= 2
    f = ImageFont.truetype(str(font_path), min_size)
    while text and draw.textlength(text + "…", font=f) > max_w:
        text = text[:-1]
    return f, (text + "…" if text else "")


class Renderer:
    def __init__(self, w, h):
        self.w, self.h = w, h
        self.bold = FONT_DIR / "DejaVuSans-Bold.ttf"
        self.regular = FONT_DIR / "DejaVuSans.ttf"
        self.margin = int(h * 0.10)
        self.art = int(h * ART_FRAC)
        self.bar_y = int(h * 0.855)
        self.strip_y = self.bar_y - 12
        self.strip_h = int(h * 0.10)
        self.accent = (235, 235, 235)

    # -- backdrop ---------------------------------------------------------
    def _backdrop(self, cover):
        if cover is None:
            return Image.new("RGB", (self.w, self.h), (14, 14, 16))
        # blur cheaply: shrink hard, blur, scale back up
        small = cover.resize((48, 48), Image.BILINEAR).filter(ImageFilter.GaussianBlur(9))
        bg = small.resize((self.w, self.h), Image.BICUBIC)
        return Image.eval(bg, lambda p: int(p * BG_DIM))

    # -- full frame -------------------------------------------------------
    def render(self, track: Track, cover: Image.Image | None) -> Image.Image:
        img = self._backdrop(cover)
        d = ImageDraw.Draw(img)
        self.accent = accent_from(cover) if cover is not None else (235, 235, 235)

        art_x, art_y = self.margin, (self.h - self.art) // 2 - int(self.h * 0.035)
        if cover is not None:
            art = cover.resize((self.art, self.art), Image.LANCZOS)
            # soft drop shadow
            shadow = Image.new("RGBA", (self.art + 48, self.art + 48), (0, 0, 0, 0))
            ImageDraw.Draw(shadow).rectangle(
                [24, 28, self.art + 24, self.art + 30], fill=(0, 0, 0, 150))
            shadow = shadow.filter(ImageFilter.GaussianBlur(14))
            img.paste(shadow, (art_x - 24, art_y - 24), shadow)
            img.paste(art, (art_x, art_y))
        else:
            d.rectangle([art_x, art_y, art_x + self.art, art_y + self.art],
                        fill=(34, 34, 38))

        tx = art_x + self.art + int(self.w * 0.045)
        tw = self.w - tx - self.margin
        y = art_y + int(self.h * 0.03)

        if not track.title:
            f = ImageFont.truetype(str(self.bold), 46)
            d.text((tx, y), "Ready", font=f, fill=(245, 245, 245))
            f2 = ImageFont.truetype(str(self.regular), 30)
            d.text((tx, y + 64),
                   f"Select “{track.device_name or 'this device'}” in Spotify",
                   font=f2, fill=(165, 165, 172))
            return img

        f_title, title = fit_font(d, track.title, self.bold, 58, tw, 30)
        d.text((tx, y), title, font=f_title, fill=(250, 250, 250))
        y += f_title.size + int(self.h * 0.028)

        f_art, artist = fit_font(d, track.artist, self.regular, 40, tw, 22)
        d.text((tx, y), artist, font=f_art, fill=self.accent)
        y += f_art.size + int(self.h * 0.022)

        if track.album:
            f_alb, album = fit_font(d, track.album, self.regular, 29, tw, 18)
            d.text((tx, y), album, font=f_alb, fill=(158, 158, 166))

        return img

    # -- progress strip ---------------------------------------------------
    def _draw_progress(self, img, track: Track, y_offset: int = 0):
        d = ImageDraw.Draw(img)
        x0, x1 = self.margin, self.w - self.margin
        y = self.bar_y + y_offset
        h = 7
        d.rounded_rectangle([x0, y, x1, y + h], radius=h // 2, fill=(72, 72, 78))

        if track.duration_ms > 0:
            frac = max(0.0, min(1.0, track.elapsed_ms() / track.duration_ms))
            if frac > 0:
                d.rounded_rectangle([x0, y, x0 + int((x1 - x0) * frac), y + h],
                                    radius=h // 2, fill=self.accent)
        f = ImageFont.truetype(str(self.regular), 24)
        d.text((x0, y + 20), fmt_ms(track.elapsed_ms()), font=f, fill=(170, 170, 178))
        dur = fmt_ms(track.duration_ms)
        d.text((x1 - d.textlength(dur, font=f), y + 20), dur,
               font=f, fill=(170, 170, 178))

        if track.status != "playing":
            label = track.status.upper()
            fs = ImageFont.truetype(str(self.bold), 22)
            d.text(((self.w - d.textlength(label, font=fs)) / 2, y + 20),
                   label, font=fs, fill=(200, 200, 60))

    def compose(self, base: Image.Image, track: Track) -> Image.Image:
        """Full frame: clean base plus the current progress bar."""
        img = base.copy()
        self._draw_progress(img, track)
        return img

    def progress_strip(self, base: Image.Image, track: Track) -> Image.Image:
        """Just the progress rows, cropped from the clean base - cheap enough
        to repaint every second without touching the rest of the screen."""
        strip = base.crop((0, self.strip_y, self.w, self.strip_y + self.strip_h))
        self._draw_progress(strip, track, y_offset=-self.strip_y)
        return strip


def fmt_ms(ms):
    s = int(max(0, ms) // 1000)
    return f"{s // 60}:{s % 60:02d}"


# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------
def ws_url():
    addr_f, port_f = STATE_DIR / "ws.addr", STATE_DIR / "ws.port"
    addr = addr_f.read_text().strip() if addr_f.exists() else "127.0.0.1"
    port = port_f.read_text().strip() if port_f.exists() else "3678"
    return f"ws://{addr}:{port}"


async def run():
    fb = Framebuffer()
    rend = Renderer(fb.width, fb.height)
    track = Track()
    cover = None
    base = None
    last_key = None

    async def repaint_full():
        nonlocal base
        base = rend.render(track, cover)
        fb.blit(rend.compose(base, track))

    await repaint_full()

    async def ticker():
        while True:
            await asyncio.sleep(TICK_SECONDS)
            if base is not None and track.title and track.duration_ms:
                try:
                    fb.blit_rows(rend.progress_strip(base, track), rend.strip_y)
                except Exception as e:
                    LOG.warning("progress update failed: %s", e)

    asyncio.create_task(ticker())

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
                        if track.cover_url:
                            new = await asyncio.to_thread(fetch_cover, track.cover_url)
                            if new is not None:
                                cover = new
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
