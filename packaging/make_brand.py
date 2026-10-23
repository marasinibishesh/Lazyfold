"""Draw SlothFile's brand assets from one design:
  packaging/slothfile.ico   multi-size app icon (16-256 px)
  packaging/slothfile.icns  macOS app icon (16-1024 px), for the .app bundle
  packaging/slothfile.png   256 px icon
  packaging/logo-512.png    512 px logo
  packaging/splash.png      splash screen shown while the app starts
Run: python packaging/make_brand.py  (Pillow; Segoe UI from Windows for the wordmark)
"""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
GREEN_TOP, GREEN_BOTTOM = (64, 140, 100), (30, 86, 60)
CREAM, BROWN, DARK = (246, 236, 220), (92, 70, 54), (52, 38, 28)


def _version() -> str:
    for line in (ROOT / "slothfile" / "__init__.py").read_text(encoding="utf-8").splitlines():
        if line.startswith("__version__"):
            return line.split("=")[1].strip().strip('"')
    return ""


def _rot_ellipse(size: int, box, angle: float, fill) -> Image.Image:
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(layer).ellipse(box, fill=fill)
    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    return layer.rotate(angle, center=(cx, cy), resample=Image.BICUBIC)


def logo(size: int = 1024) -> Image.Image:
    """The mark: a calm sloth face on a green rounded tile."""
    s = size
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    # Tile with a soft vertical gradient.
    grad = Image.new("RGBA", (s, s))
    gd = ImageDraw.Draw(grad)
    for y in range(s):
        t = y / (s - 1)
        gd.line([(0, y), (s, y)], fill=tuple(round(a + (b - a) * t) for a, b in zip(GREEN_TOP, GREEN_BOTTOM)) + (255,))
    mask = Image.new("L", (s, s), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0.03 * s, 0.03 * s, 0.97 * s, 0.97 * s], radius=0.23 * s, fill=255)
    img.paste(grad, (0, 0), mask)
    # Face.
    d = ImageDraw.Draw(img)
    d.ellipse([0.17 * s, 0.22 * s, 0.83 * s, 0.82 * s], fill=CREAM + (255,))
    # Eye bands slope down and outward, the sloth's signature.
    for cx, ang in ((0.355, 14), (0.645, -14)):
        band = _rot_ellipse(s, [(cx - 0.125) * s, 0.43 * s, (cx + 0.125) * s, 0.575 * s], ang, BROWN + (255,))
        img.alpha_composite(band)
    d = ImageDraw.Draw(img)
    for cx in (0.36, 0.64):  # closed, content eyes
        d.arc([(cx - 0.055) * s, 0.465 * s, (cx + 0.055) * s, 0.545 * s], start=20, end=160,
              fill=CREAM + (255,), width=max(2, round(0.024 * s)))
    d.ellipse([0.462 * s, 0.585 * s, 0.538 * s, 0.638 * s], fill=DARK + (255,))  # nose
    d.arc([0.43 * s, 0.6 * s, 0.57 * s, 0.705 * s], start=35, end=145, fill=DARK + (255,),
          width=max(2, round(0.016 * s)))
    return img


def _font(name: str, px: int) -> ImageFont.FreeTypeFont:
    for path in (Path("C:/Windows/Fonts") / name, Path("C:/Windows/Fonts/segoeui.ttf")):
        if path.exists():
            return ImageFont.truetype(str(path), px)
    return ImageFont.load_default()


def splash(w: int = 640, h: int = 360) -> Image.Image:
    sc = 2  # draw at 2x, then downscale for smooth edges
    W, H = w * sc, h * sc
    img = Image.new("RGB", (W, H), (250, 249, 246))
    d = ImageDraw.Draw(img)
    # Soft brand glow behind the logo.
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse([W * 0.06, H * 0.12, W * 0.46, H * 0.88], fill=GREEN_TOP + (38,))
    img.paste(glow.filter(ImageFilter.GaussianBlur(60 * sc)), (0, 0), glow.filter(ImageFilter.GaussianBlur(60 * sc)))
    mark = logo(1024).resize((150 * sc, 150 * sc), Image.LANCZOS)
    img.paste(mark, (int(W * 0.13), int((H - mark.height) / 2) - 10 * sc), mark)
    x = int(W * 0.13) + mark.width + 34 * sc
    d.text((x, H * 0.33), "SlothFile", font=_font("seguisb.ttf", 46 * sc), fill=(27, 27, 27))
    d.text((x, H * 0.33 + 62 * sc), "Tidy files safely, right on your PC", font=_font("segoeui.ttf", 17 * sc),
           fill=(96, 96, 96))
    d.text((x, H * 0.33 + 96 * sc), "Starting...", font=_font("segoeui.ttf", 13 * sc), fill=(140, 140, 140))
    foot = f"by Bishesh Marasini   ·   version {_version()}"
    d.text((28 * sc, H - 40 * sc), foot, font=_font("segoeui.ttf", 12 * sc), fill=(150, 150, 150))
    d.rectangle([0, H - 6 * sc, W, H], fill=GREEN_BOTTOM)
    return img.resize((w, h), Image.LANCZOS)


def write_icns(big: Image.Image, path: Path = HERE / "slothfile.icns") -> None:
    """macOS icon: Pillow writes every ICNS size (16-1024 px, with @2x) from
    one 1024 px image. Also runnable on its own, from the committed logo:
    python -c "from make_brand import *; write_icns(Image.open(HERE / 'logo-512.png'))"."""
    big.convert("RGBA").resize((1024, 1024), Image.LANCZOS).save(path, format="ICNS")


if __name__ == "__main__":
    big = logo()
    big.save(HERE / "slothfile.ico", sizes=[(16, 16), (20, 20), (24, 24), (32, 32), (40, 40), (48, 48),
                                            (64, 64), (128, 128), (256, 256)])
    write_icns(big)
    big.resize((256, 256), Image.LANCZOS).save(HERE / "slothfile.png")
    big.resize((512, 512), Image.LANCZOS).save(HERE / "logo-512.png")
    splash().save(HERE / "splash.png")
    print("wrote icon, logos and splash in", HERE)
