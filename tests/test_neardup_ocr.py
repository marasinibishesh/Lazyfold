"""Near-duplicates, Windows OCR and photos-by-date. Everything happens in pytest
tmp dirs; nothing is deleted, recycled or moved outside them."""

import builtins
import os
import subprocess
import sys
from pathlib import Path

import pytest

from lazyfold import neardup, ocr, photos
from lazyfold.demo import make_pdf
from lazyfold.models import FileRecord
from lazyfold.scanner import scan_folder

from conftest import write_old

PIL = pytest.importorskip("PIL")
from PIL import Image, ImageDraw, ImageEnhance, ImageFont  # noqa: E402


def _rec(rel: str, size: int = 100, **kw) -> FileRecord:
    ext = os.path.splitext(rel)[1].lower()
    return FileRecord(rel_path=rel, name=os.path.basename(rel), ext=ext, size=size,
                      mtime_ns=kw.pop("mtime_ns", 1), mtime_iso="", **kw)


def _scene(seed: int = 0, size=(320, 240)) -> Image.Image:
    """A photo-like picture with structure (a flat colour would hash to 0)."""
    im = Image.new("RGB", size, (30, 60, 90))
    d = ImageDraw.Draw(im)
    w, h = size
    for x in range(w):
        d.line([(x, 0), (x, h)], fill=((x * 255) // w, 80, 200 - (x * 150) // w))
    if seed == 0:
        d.ellipse([w * 0.1, h * 0.2, w * 0.5, h * 0.8], fill=(250, 240, 30))
        d.rectangle([w * 0.6, h * 0.1, w * 0.9, h * 0.5], fill=(10, 10, 10))
    else:
        for i in range(0, w, 40):
            d.rectangle([i, 0, i + 20, h], fill=(255, 255, 255))
        d.ellipse([w * 0.55, h * 0.5, w * 0.95, h * 0.95], fill=(200, 0, 0))
    return im


def _font(size: int = 48):
    try:
        return ImageFont.truetype("arial.ttf", size)
    except OSError:
        return ImageFont.load_default()


def _text_png(path: Path, lines: list[str]) -> Path:
    im = Image.new("RGB", (1200, 120 + 80 * len(lines)), "white")
    d = ImageDraw.Draw(im)
    for i, line in enumerate(lines):
        d.text((40, 40 + 80 * i), line, fill="black", font=_font())
    im.save(path)
    return path


# ---------------------------------------------------------------- neardup

def test_dhash_and_hamming_basics(tmp_path):
    p = tmp_path / "a.png"
    _scene().save(p)
    h = neardup.dhash(p)
    assert isinstance(h, int) and 0 <= h < 2 ** 64
    assert neardup.hamming(h, h) == 0
    assert neardup.hamming(0b1010, 0b0101) == 4
    bad = tmp_path / "bad.png"
    bad.write_bytes(b"\x89PNG\r\n\x1a\nnot really")
    assert neardup.dhash(bad) is None


def test_near_identical_images_detected(tmp_path):
    big = _scene()
    big.save(tmp_path / "holiday.png")
    small = ImageEnhance.Brightness(big.resize((240, 180))).enhance(1.08)
    small.convert("RGB").save(tmp_path / "holiday (1).jpg", quality=85)
    _scene(seed=1).save(tmp_path / "other.png")
    recs = [
        _rec("holiday.png", size=(tmp_path / "holiday.png").stat().st_size,
             sniffed="png", meta={"width": 320, "height": 240}),
        _rec("holiday (1).jpg", size=(tmp_path / "holiday (1).jpg").stat().st_size,
             sniffed="jpeg", meta={"width": 240, "height": 180}),
        _rec("other.png", size=(tmp_path / "other.png").stat().st_size, sniffed="png",
             meta={"width": 320, "height": 240}),
    ]
    found = neardup.find_near_duplicates(tmp_path, recs)
    assert set(found) == {"holiday (1).jpg"}
    kept, reason = found["holiday (1).jpg"]
    assert kept == "holiday.png"  # more pixels wins
    assert "same picture as holiday.png" in reason and "perceptual distance" in reason


def test_very_different_images_not_matched(tmp_path):
    _scene(0).save(tmp_path / "a.png")
    _scene(1).save(tmp_path / "b.png")
    ha, hb = neardup.dhash(tmp_path / "a.png"), neardup.dhash(tmp_path / "b.png")
    assert neardup.hamming(ha, hb) > 4
    recs = [_rec("a.png", sniffed="png"), _rec("b.png", sniffed="png")]
    assert neardup.find_near_duplicates(tmp_path, recs) == {}


def test_exact_duplicates_and_skipped_records_excluded(tmp_path):
    _scene().save(tmp_path / "a.png")
    (tmp_path / "a - Copy.png").write_bytes((tmp_path / "a.png").read_bytes())
    recs = [_rec("a.png", sniffed="png", sha256="x"),
            _rec("a - Copy.png", sniffed="png", sha256="x")]
    assert neardup.find_near_duplicates(tmp_path, recs) == {}
    # A skipped or disguised file is never offered as a near-copy.
    recs = [_rec("a.png", sniffed="png"),
            _rec("a - Copy.png", sniffed="png", skip_reason="in use")]
    assert neardup.find_near_duplicates(tmp_path, recs) == {}
    recs = [_rec("a.png", sniffed="png"), _rec("a - Copy.png", sniffed="png", ext_mismatch=True)]
    assert neardup.find_near_duplicates(tmp_path, recs) == {}


LETTER = [
    "Brightwater Logistics Ltd - Statement of account for October 2026",
    "Customer number 448812, account holder Jordan Example, 12 Harbour Road",
    "Opening balance 1,204.50 and closing balance 980.10 after three payments",
    "Payment received 14 October 2026 by bank transfer, reference BW-77812",
    "Please contact accounts at Brightwater if anything on this page is wrong",
]


def test_same_text_pdfs_with_different_metadata(tmp_path):
    root = tmp_path / "Downloads"
    write_old(root / "statement.pdf", make_pdf(LETTER, title="Statement"), days=20)
    write_old(root / "statement (1).pdf", make_pdf(LETTER, title="Statement - downloaded again"), days=5)
    write_old(root / "unrelated.pdf", make_pdf([l[::-1] for l in LETTER]), days=5)
    records, _ = scan_folder(root)
    if not any(r.text for r in records):
        pytest.skip("pypdf not installed: no PDF text to compare")
    found = neardup.find_near_duplicates(root, records)
    assert set(found) == {"statement (1).pdf"}
    kept, reason = found["statement (1).pdf"]
    assert kept == "statement.pdf"
    assert "same text as statement.pdf but different file bytes" in reason


def test_same_text_exact_bytes_not_reported():
    text = " ".join(LETTER)
    recs = [_rec("a.pdf", text=text, sha256="s"), _rec("b.pdf", text=text, sha256="s")]
    assert neardup.find_near_duplicates(Path("."), recs) == {}


def test_text_fingerprint_normalizes_and_needs_enough_text():
    a = " ".join(LETTER)
    b = "  " + a.upper().replace(" ", " \t ").replace("-", "!") + "?!\n\n"
    assert neardup.text_fingerprint(a) == neardup.text_fingerprint(b)
    assert neardup.text_fingerprint("Invoice 42") is None
    assert neardup.text_fingerprint("") is None


def test_pillow_missing_degrades(tmp_path, monkeypatch):
    _scene().save(tmp_path / "a.png")
    real_import = builtins.__import__

    def no_pil(name, *a, **kw):
        if name == "PIL" or name.startswith("PIL."):
            raise ImportError("no Pillow here")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", no_pil)
    assert neardup.dhash(tmp_path / "a.png") is None
    recs = [_rec("a.png", sniffed="png"), _rec("b.png", sniffed="png")]
    assert neardup.find_near_duplicates(tmp_path, recs) == {}


def test_neardup_refuses_escaping_paths(tmp_path):
    recs = [_rec(r"..\outside.png", sniffed="png"), _rec(r"C:\Windows\x.png", sniffed="png")]
    assert neardup.find_near_duplicates(tmp_path, recs) == {}


# ---------------------------------------------------------------- OCR

needs_ocr = pytest.mark.skipif(not (sys.platform == "win32" and ocr.ocr_available()),
                               reason="Windows OCR engine not available")


@needs_ocr
def test_ocr_reads_rendered_text(tmp_path):
    p = _text_png(tmp_path / "receipt.png", ["INVOICE NUMBER 4471", "Brightwater Logistics"])
    out = ocr.ocr_images([p])
    text = out[str(p)].casefold()
    assert "invoice" in text and "4471" in text and "brightwater" in text


@needs_ocr
def test_ocr_batch_reports_bad_image_without_losing_good_one(tmp_path):
    good = _text_png(tmp_path / "good.png", ["HELLO LAZYFOLD"])
    bad = tmp_path / "bad.png"
    bad.write_bytes(b"not an image at all")
    out = ocr.ocr_images([good, bad, tmp_path / "missing.png"])
    assert "lazyfold" in out[str(good)].casefold()
    assert str(bad) not in out and str(tmp_path / "missing.png") not in out


@needs_ocr
def test_ocr_scanned_pdf(tmp_path):
    png = _text_png(tmp_path / "page.png", ["PAYSLIP SEPTEMBER 2026", "Net pay 2150"])
    pdf = tmp_path / "Scan_20261003.pdf"
    Image.open(png).convert("RGB").save(pdf)  # image-only PDF, like a scanner makes
    assert ocr.pdf_page_images(pdf)
    work = tmp_path / "ocrwork"
    work.mkdir()
    sentinel = work / "keep.txt"
    sentinel.write_text("not ours")
    text = ocr.ocr_pdf(pdf, work).casefold()
    assert "payslip" in text and "2150" in text
    assert [p.name for p in work.iterdir()] == ["keep.txt"]  # only our files removed


@needs_ocr
def test_ocr_hostile_filename_is_not_executed(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # if anything were evaluated, it would land here
    marker = tmp_path / "pwned.txt"
    name = "a'b; $(New-Item pwned.txt) `& ${X} %PATH% calc;'x [1].png"
    p = _text_png(tmp_path / name, ["SAFE NAME TEST"])
    out = ocr.ocr_images([p])
    assert not marker.exists()
    assert str(p) in out and "safe" in out[str(p)].casefold()


@needs_ocr
def test_ocr_scales_down_oversized_image(tmp_path):
    big = Image.new("RGB", (12000, 600), "white")
    ImageDraw.Draw(big).text((40, 100), "OVERSIZED SCAN", fill="black", font=_font(200))
    p = tmp_path / "wide.png"
    big.save(p)
    out = ocr.ocr_images([p])
    assert "oversized" in out.get(str(p), "").casefold()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only behaviour")
def test_ocr_timeout_returns_empty(tmp_path, monkeypatch):
    def boom(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="powershell", timeout=1)

    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(sys, "platform", "win32")
    assert ocr.ocr_images([tmp_path / "x.png"], timeout=1) == {}


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only behaviour")
def test_ocr_command_never_uses_shell(tmp_path, monkeypatch):
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"], seen["kw"] = cmd, kw
        return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(sys, "platform", "win32")
    if ocr._powershell() is None:
        pytest.skip("no powershell.exe")
    p = tmp_path / "a;b $(x).png"
    ocr.ocr_images([p])
    assert isinstance(seen["cmd"], list) and not seen["kw"].get("shell")
    assert seen["cmd"][-1] == os.path.abspath(p)  # one argv entry, untouched
    assert seen["cmd"][seen["cmd"].index("-File") + 1].endswith("ocr.ps1")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only behaviour")
def test_ocr_parses_json_lines_and_caps_text(tmp_path, monkeypatch):
    p = tmp_path / "a.png"
    q = tmp_path / "b.png"
    import json
    lines = [json.dumps({"path": os.path.abspath(p), "text": "x" * 9000, "error": None}),
             "garbage line", json.dumps({"path": os.path.abspath(q), "text": "", "error": "bad"})]

    def fake_run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 0, stdout="\n".join(lines).encode(), stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(sys, "platform", "win32")
    out = ocr.ocr_images([p, q])
    assert list(out) == [str(p)] and len(out[str(p)]) == ocr.MAX_TEXT


def test_pdf_page_images_text_pdf_has_none(tmp_path):
    p = tmp_path / "t.pdf"
    p.write_bytes(make_pdf(LETTER))
    assert ocr.pdf_page_images(p) == []
    junk = tmp_path / "junk.pdf"
    junk.write_bytes(b"%PDF-1.4 broken")
    assert ocr.pdf_page_images(junk) == []
    assert ocr.ocr_pdf(junk, tmp_path) == ""


# ---------------------------------------------------------------- photos

@pytest.mark.parametrize("taken,expected", [
    ("2026:10:03 14:22:01", (2026, 10)),
    ("1990:01:31 00:00:00", (1990, 1)),
    ("0000:00:00 00:00:00", None),
    ("1980:01:01 00:00:00", None),
    ("2026:13:01 00:00:00", None),
    ("2101:01:01 00:00:00", None),
    ("garbage", None),
    ("２０２６:10:03", None),
])
def test_photo_date(taken, expected):
    assert photos.photo_date(_rec("p.jpg", meta={"taken": taken})) == expected


def test_photo_date_ignores_mtime():
    assert photos.photo_date(_rec("p.jpg", mtime_ns=1_790_000_000 * 10**9)) is None
    assert photos.photo_date(_rec("p.jpg", meta={"taken": 20261003})) is None


def test_dated_subfolder_and_validator():
    rel = photos.dated_subfolder(2026, 3)
    assert rel == str(Path("Images", "2026", "03"))
    assert photos.is_dated_image_folder(rel)
    for ok in ("images/2026/10", "IMAGES\\1999\\12"):
        assert photos.is_dated_image_folder(ok)
    for bad in ("Images/2026", "Images/2026/10/x", "Images/2026/13", "Images/26/10",
                "Images/2026/1", "Images/1989/05", "Pictures/2026/10", "Images/../10",
                "Images/2026/10\n", "", "Images/２０２６/10", "/Images/2026/10"):
        assert not photos.is_dated_image_folder(bad), bad


def test_photo_date_from_real_exif(tmp_path):
    from lazyfold.extract import extract_image
    im = _scene()
    exif = Image.Exif()
    exif.get_ifd(0x8769)[36867] = "2025:07:14 09:30:00"  # DateTimeOriginal (capture date)
    p = tmp_path / "IMG_0001.jpg"
    im.save(p, exif=exif)
    _, meta = extract_image(p)
    assert photos.photo_date(_rec("IMG_0001.jpg", meta=meta)) == (2025, 7)


def test_edit_date_alone_is_not_a_capture_date(tmp_path):
    from lazyfold.extract import extract_image
    exif = Image.Exif()
    exif[306] = "2025:07:14 09:30:00"  # DateTime = last edited, not taken
    p = tmp_path / "edited.jpg"
    _scene().save(p, exif=exif)
    _, meta = extract_image(p)
    assert photos.photo_date(_rec("edited.jpg", meta=meta)) is None and meta.get("modified")


# ---------------------------------------------------------------- regressions

def _noise(size=(320, 240), seed: int = 7) -> Image.Image:
    import random
    rnd = random.Random(seed)
    w, h = 16, 12
    im = Image.new("RGB", (w, h))
    im.putdata([(rnd.randrange(256), rnd.randrange(256), rnd.randrange(256))
                for _ in range(w * h)])
    return im.resize(size, Image.Resampling.BICUBIC)


def _img_recs(tmp_path, names):
    recs = []
    for n in names:
        with Image.open(tmp_path / n) as im:
            w, h = im.size
        recs.append(_rec(n, size=(tmp_path / n).stat().st_size, sniffed="png",
                         meta={"width": w, "height": h}))
    return recs


def test_flat_and_gradient_images_are_not_the_same_picture(tmp_path):
    Image.new("RGB", (200, 150), (220, 20, 20)).save(tmp_path / "red.png")
    Image.new("RGB", (200, 150), (20, 20, 220)).save(tmp_path / "blue.png")
    Image.new("RGB", (200, 150), (0, 0, 0)).save(tmp_path / "black.png")
    grad = Image.linear_gradient("L").resize((200, 150)).convert("RGB")
    grad.save(tmp_path / "vgrad.png")
    grad.rotate(90, expand=True).resize((200, 150)).save(tmp_path / "hgrad.png")
    names = ["red.png", "blue.png", "black.png", "vgrad.png", "hgrad.png"]
    assert neardup.dhash(tmp_path / "red.png") == 0  # public API unchanged
    assert neardup.find_near_duplicates(tmp_path, _img_recs(tmp_path, names)) == {}


def test_same_hash_but_different_colour_or_shape_not_matched(tmp_path):
    from PIL import ImageOps
    base = _noise()
    base.save(tmp_path / "a.png")
    # Same structure, very different colours (hash nearly identical).
    ImageOps.colorize(base.convert("L"), (0, 0, 120), (120, 120, 255)).save(tmp_path / "tinted.png")
    assert neardup.hamming(neardup.dhash(tmp_path / "a.png"),
                           neardup.dhash(tmp_path / "tinted.png")) <= 4
    # Same picture squashed to a different shape.
    base.resize((320, 120)).save(tmp_path / "squashed.png")
    recs = _img_recs(tmp_path, ["a.png", "tinted.png", "squashed.png"])
    assert neardup.find_near_duplicates(tmp_path, recs) == {}


def test_tiny_images_skipped(tmp_path):
    _noise((24, 24)).save(tmp_path / "icon.png")
    _noise((24, 24)).save(tmp_path / "icon2.bmp")
    recs = _img_recs(tmp_path, ["icon.png", "icon2.bmp"])
    assert neardup.find_near_duplicates(tmp_path, recs) == {}


def test_noise_photo_near_duplicate_still_matched(tmp_path):
    big = _noise()
    big.save(tmp_path / "photo.png")
    ImageEnhance.Brightness(big.resize((240, 180))).enhance(1.06).save(
        tmp_path / "photo (1).jpg", quality=85)
    recs = _img_recs(tmp_path, ["photo.png", "photo (1).jpg"])
    found = neardup.find_near_duplicates(tmp_path, recs)
    assert set(found) == {"photo (1).jpg"} and found["photo (1).jpg"][0] == "photo.png"


def _jl(path, text="t"):
    import json
    return json.dumps({"path": os.path.abspath(path), "text": text, "error": None})


def test_ocr_batches_capped_and_timeout_keeps_partial_results(tmp_path, monkeypatch):
    if ocr._powershell() is None:
        pytest.skip("no powershell.exe")
    paths = [tmp_path / f"img{i:02d}.png" for i in range(40)]
    calls = []

    def fake_run(cmd, timeout=None, **kw):
        batch = cmd[cmd.index("-File") + 2:]
        calls.append((len(batch), timeout))
        n = len(calls)
        if n == 1:  # first batch finishes
            out = "\n".join(_jl(p) for p in batch).encode()
            return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr=b"")
        if n == 2:  # second batch times out after 3 images and half a line
            partial = "\n".join(_jl(p) for p in batch[:3]) + "\n" + _jl(batch[3])[:20]
            raise subprocess.TimeoutExpired(cmd, timeout, output=partial.encode())
        raise subprocess.TimeoutExpired(cmd, timeout, output=None)  # nothing at all

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(sys, "platform", "win32")
    out = ocr.ocr_images(paths, timeout=5)
    assert [c[0] for c in calls] == [16, 16, 8]
    assert [c[1] for c in calls] == [42, 42, 26]  # max(timeout, 10 + 2 * len(batch))
    assert set(out) == {str(p) for p in paths[:19]}


def test_ocr_timeout_floor_respects_larger_timeout(tmp_path, monkeypatch):
    if ocr._powershell() is None:
        pytest.skip("no powershell.exe")
    seen = []

    def fake_run(cmd, timeout=None, **kw):
        seen.append(timeout)
        return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(sys, "platform", "win32")
    ocr.ocr_images([tmp_path / "a.png"], timeout=300)
    assert seen == [300]


def test_ocr_available_retries_once_after_timeout(monkeypatch):
    if ocr._powershell() is None:
        pytest.skip("no powershell.exe")
    calls = []
    mode = {"always_timeout": False}

    def fake_run(cmd, timeout=None, **kw):
        calls.append(1)
        if mode["always_timeout"] or len(calls) == 1:
            raise subprocess.TimeoutExpired(cmd, timeout)
        return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(subprocess, "run", fake_run)
    ocr.ocr_available.cache_clear()
    try:
        assert ocr.ocr_available() is False  # cold start timed out: not cached
        assert ocr.ocr_available() is True  # retried
        assert ocr.ocr_available() is True and len(calls) == 2  # now cached
        # Two timeouts in a row: give up and cache False.
        ocr.ocr_available.cache_clear()
        calls.clear()
        mode["always_timeout"] = True
        assert ocr.ocr_available() is False
        assert ocr.ocr_available() is False
        assert ocr.ocr_available() is False and len(calls) == 2
    finally:
        ocr.ocr_available.cache_clear()


def _image_pdf(n_refs: int, w: int, h: int, n_distinct: int = 1) -> bytes:
    """Hand-built PDF (same shape as the adversarial pdf_dos.py): one page whose
    /XObject dict names `n_refs` images pointing at `n_distinct` image objects
    of w x h gray pixels."""
    import zlib
    data = zlib.compress(b"\x80" * (w * h), 9)
    xo = " ".join(f"/Im{i} {5 + (i % n_distinct)} 0 R" for i in range(n_refs))
    c = b"q 612 0 0 792 0 0 cm /Im0 Do Q"
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
             f"/Resources << /XObject << {xo} >> >> /Contents 4 0 R >>").encode(),
            f"<< /Length {len(c)} >>\nstream\n".encode() + c + b"\nendstream"]
    for _ in range(n_distinct):
        objs.append((f"<< /Type /XObject /Subtype /Image /Width {w} /Height {h} "
                     f"/ColorSpace /DeviceGray /BitsPerComponent 8 /Filter /FlateDecode "
                     f"/Length {len(data)} >>\nstream\n").encode() + data + b"\nendstream")
    out = bytearray(b"%PDF-1.4\n")
    offs = []
    for i, o in enumerate(objs, 1):
        offs.append(len(out))
        out += f"{i} 0 obj\n".encode() + o + b"\nendobj\n"
    x = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offs)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{x}\n%%EOF\n".encode()
    return bytes(out)


def test_pdf_page_images_bounded(tmp_path):
    pytest.importorskip("pypdf")
    import time
    # 50 names for one 49 MP image: declared size is over the cap, never decoded.
    bomb = tmp_path / "bomb.pdf"
    bomb.write_bytes(_image_pdf(50, 7000, 7000))
    t = time.time()
    assert ocr.pdf_page_images(bomb) == []
    assert time.time() - t < 10
    # 50 names for one small image: decoded once.
    rep = tmp_path / "rep.pdf"
    rep.write_bytes(_image_pdf(50, 64, 64))
    imgs = ocr.pdf_page_images(rep)
    assert len(imgs) == 1 and imgs[0][0] in ocr.DECODABLE
    # Many distinct images: capped at max_images.
    many = tmp_path / "many.pdf"
    many.write_bytes(_image_pdf(20, 32, 32, n_distinct=20))
    assert len(ocr.pdf_page_images(many)) == 8
    assert len(ocr.pdf_page_images(many, max_images=3)) == 3
