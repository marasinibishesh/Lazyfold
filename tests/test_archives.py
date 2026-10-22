import hashlib
import io
import stat
import struct
import zipfile
from pathlib import Path

import pytest

from slothfile import archives
from slothfile.archives import ArchiveError, extract_zip, inspect_zip, remove_extracted


def make_zip(path: Path, members: dict[str, bytes], compression=zipfile.ZIP_DEFLATED) -> Path:
    with zipfile.ZipFile(path, "w", compression) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return path


def raw_zip_with_name(path: Path, name: str, data: bytes = b"evil") -> Path:
    """ZipFile.writestr keeps '../' and drive letters in ZipInfo names as given."""
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(zipfile.ZipInfo(name), data)
    return path


def tree(root: Path) -> set[str]:
    return {str(p.relative_to(root)) for p in root.rglob("*")}


def test_normal_extraction_and_hashes(tmp_path):
    data = {"readme.txt": b"hello", "sub/dir/a.bin": bytes(range(256)) * 10, "sub/": b""}
    z = make_zip(tmp_path / "pack.zip", {k: v for k, v in data.items() if not k.endswith("/")})
    check = inspect_zip(z)
    assert check.ok and check.entries == 2 and not check.encrypted
    seen = []
    dest = tmp_path / "out" / "pack"
    (tmp_path / "out").mkdir()
    files = extract_zip(z, dest, on_file=lambda r, s, h: seen.append((r, s, h)))
    assert {f.rel for f in files} == {"readme.txt", str(Path("sub/dir/a.bin"))}
    for f in files:
        content = (dest / f.rel).read_bytes()
        assert hashlib.sha256(content).hexdigest() == f.sha256 and len(content) == f.size
    assert seen == [(f.rel, f.size, f.sha256) for f in files]


@pytest.mark.parametrize("name", ["../evil.txt", "C:/x", "/abs", "a/../../evil.txt", "..\\evil.txt",
                                  "ok.txt:stream", "CON", "dir/nul.txt"])
def test_unsafe_names_rejected_and_never_written(tmp_path, name):
    work = tmp_path / "work"
    work.mkdir()
    z = raw_zip_with_name(tmp_path / "bad.zip", name)
    check = inspect_zip(z)
    assert not check.ok and "unsafe name" in check.reason
    with pytest.raises(ArchiveError) as ei:
        extract_zip(z, work / "dest")
    assert ei.value.extracted == []
    assert not (work / "dest").exists()
    assert not (tmp_path / "evil.txt").exists() and not (work / "evil.txt").exists()
    assert tree(tmp_path) - {"state"} == {"work", "bad.zip"}
    assert list(work.iterdir()) == []


def test_zip_bomb_ratio_rejected(tmp_path):
    z = make_zip(tmp_path / "bomb.zip", {"zeros.bin": b"\0" * (5 * 1024 * 1024)})
    check = inspect_zip(z)
    assert not check.ok and "zip bomb" in check.reason


def test_small_compressible_file_is_fine(tmp_path):
    z = make_zip(tmp_path / "spaces.zip", {"blank.txt": b" " * 20000})
    assert inspect_zip(z).ok


def test_encrypted_member_rejected(tmp_path):
    z = tmp_path / "enc.zip"
    info = zipfile.ZipInfo("secret.txt")
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr(info, b"data")
    # Flip the "encrypted" flag bit in both headers, as a password-protected zip has.
    raw = bytearray(z.read_bytes())
    for sig, off in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
        i = raw.find(sig)
        flags = struct.unpack_from("<H", raw, i + off)[0]
        struct.pack_into("<H", raw, i + off, flags | 0x1)
    z.write_bytes(bytes(raw))
    check = inspect_zip(z)
    assert not check.ok and check.encrypted and "password" in check.reason


def test_symlink_member_rejected(tmp_path):
    z = tmp_path / "link.zip"
    info = zipfile.ZipInfo("link")
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr(info, "/etc/passwd")
    check = inspect_zip(z)
    assert not check.ok and "link" in check.reason


def test_duplicate_names_case_insensitive_rejected(tmp_path):
    z = tmp_path / "dup.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("Report.txt", b"a")
        zf.writestr("report.TXT", b"b")
    assert "duplicate" in inspect_zip(z).reason


def test_too_many_entries_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(archives, "MAX_ENTRIES", 3)
    z = make_zip(tmp_path / "many.zip", {f"f{i}.txt": b"x" for i in range(4)})
    assert "too many" in inspect_zip(z).reason


def test_dest_exists_is_error(tmp_path):
    z = make_zip(tmp_path / "p.zip", {"a.txt": b"a"})
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "a.txt").write_bytes(b"mine")
    with pytest.raises(ArchiveError, match="already exists"):
        extract_zip(z, dest)
    assert (dest / "a.txt").read_bytes() == b"mine"


def test_size_lie_aborted(tmp_path):
    """Member data longer than its declared size: the extra bytes are never
    accepted and the half-written file is removed."""
    z = tmp_path / "lie.zip"
    with zipfile.ZipFile(z, "w", zipfile.ZIP_STORED) as zf:
        zf.writestr("first.txt", b"fine")
        zf.writestr("big.txt", b"A" * 1000)
    raw = bytearray(z.read_bytes())
    # Declare 10 bytes for big.txt in both the local and the central header.
    li = raw.find(b"PK\x03\x04", raw.find(b"big.txt") - 40)
    struct.pack_into("<II", raw, li + 18, 10, 10)
    ci = raw.rfind(b"PK\x01\x02")
    struct.pack_into("<II", raw, ci + 20, 10, 10)
    z.write_bytes(bytes(raw))
    assert inspect_zip(z).ok  # the lie is only visible while reading
    dest = tmp_path / "dest"
    with pytest.raises(ArchiveError) as ei:
        extract_zip(z, dest)
    assert [f.rel for f in ei.value.extracted] == ["first.txt"]
    assert not (dest / "big.txt").exists()


def test_remove_extracted_keeps_user_changes(tmp_path):
    z = make_zip(tmp_path / "p.zip", {"keep/me.txt": b"original", "gone/a.txt": b"a", "gone/deep/b.txt": b"b",
                                      "top.txt": b"t"})
    dest = tmp_path / "dest"
    files = extract_zip(z, dest)
    (dest / "keep" / "me.txt").write_bytes(b"edited by user")
    removed, skipped = remove_extracted(dest, files)
    assert removed == 3
    assert len(skipped) == 1 and "changed" in skipped[0]
    assert tree(dest) == {"keep", str(Path("keep/me.txt"))}
    (dest / "keep" / "me.txt").write_bytes(b"original")
    removed, skipped = remove_extracted(dest, files)
    assert removed == 1 and not dest.exists()


def test_seven_zip_unsupported(tmp_path):
    p = tmp_path / "x.7z"
    p.write_bytes(b"7z\xbc\xaf\x27\x1c" + b"\0" * 32)
    check = inspect_zip(p)
    assert not check.ok and "not supported" in check.reason
    r = tmp_path / "y.rar"
    r.write_bytes(b"Rar!\x1a\x07\x00" + b"\0" * 32)
    assert "not supported" in inspect_zip(r).reason


def test_not_a_zip(tmp_path):
    p = tmp_path / "fake.zip"
    p.write_bytes(b"hello world")
    assert inspect_zip(p).reason == "not a zip archive"


def test_file_and_folder_with_same_name_rejected(tmp_path):
    z = tmp_path / "clash.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("a", b"file")
        zf.writestr("a/b.txt", b"child")
    assert not inspect_zip(z).ok


def test_in_memory_bytes_never_escape(tmp_path):
    """Belt and braces: even a zip that passed inspection is extracted through
    resolve_inside, so every written path is inside dest."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("x/y/z.txt", b"1")
    z = tmp_path / "ok.zip"
    z.write_bytes(buf.getvalue())
    dest = tmp_path / "d"
    for f in extract_zip(z, dest):
        assert (dest / f.rel).resolve().is_relative_to(dest.resolve())


# --- regressions -----------------------------------------------------------

def _corrupt_deflate(path: Path) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("ok.txt", b"hello")
        zf.writestr("b.txt", b"abc" * 1000)
    raw = bytearray(path.read_bytes())
    with zipfile.ZipFile(path) as zf:
        info = zf.getinfo("b.txt")
    raw[info.header_offset + 30 + len("b.txt")] = 0xFF  # BTYPE=11 (reserved): zlib.error
    path.write_bytes(bytes(raw))
    return path


def test_corrupt_deflate_data_is_archive_error_with_extracted(tmp_path):
    z = _corrupt_deflate(tmp_path / "bad.zip")
    assert inspect_zip(z).ok  # only visible while decompressing
    dest = tmp_path / "dest"
    with pytest.raises(ArchiveError, match="extraction stopped") as ei:
        extract_zip(z, dest)
    assert [f.rel for f in ei.value.extracted] == ["ok.txt"]
    assert (dest / "ok.txt").read_bytes() == b"hello"
    assert not (dest / "b.txt").exists()  # half-written file removed


def test_on_file_exception_is_archive_error_with_extracted(tmp_path):
    z = make_zip(tmp_path / "p.zip", {"a.txt": b"a", "b.txt": b"b"})
    dest = tmp_path / "dest"

    def boom(rel, size, sha):
        raise RuntimeError("journal full")

    with pytest.raises(ArchiveError, match="journal full") as ei:
        extract_zip(z, dest, on_file=boom)
    # a.txt was written before on_file failed, so the caller must hear about it.
    assert [f.rel for f in ei.value.extracted] == ["a.txt"]
    assert isinstance(ei.value.__cause__, RuntimeError)
    removed, skipped = remove_extracted(dest, ei.value.extracted)
    assert removed == 1 and skipped == [] and not dest.exists()


def test_large_zip_file_error_is_archive_error(tmp_path, monkeypatch):
    z = make_zip(tmp_path / "p.zip", {"a.txt": b"a"})
    real_open = zipfile.ZipFile.open

    def big(self, *a, **kw):
        raise zipfile.LargeZipFile("needs zip64")

    monkeypatch.setattr(zipfile.ZipFile, "open", big)
    with pytest.raises(ArchiveError, match="zip64") as ei:
        extract_zip(z, tmp_path / "dest")
    assert ei.value.extracted == []
    monkeypatch.setattr(zipfile.ZipFile, "open", real_open)


def test_extract_checks_the_handle_it_reads_from(tmp_path, monkeypatch):
    """extract_zip must not rely on a separate inspect_zip() pass (which opens
    the file again): the checks run on the very handle that is extracted."""
    z = make_zip(tmp_path / "bomb.zip", {"zeros.bin": b"\0" * (5 * 1024 * 1024)})
    # Even if inspect_zip were fooled (e.g. the file was swapped after it ran)...
    monkeypatch.setattr(archives, "inspect_zip", lambda p: archives.ZipCheck(True, "ok"))
    work = tmp_path / "work"
    work.mkdir()
    with pytest.raises(ArchiveError, match="zip bomb"):
        extract_zip(z, work / "dest")
    assert list(work.iterdir()) == []
    # ...and the archive is opened exactly once.
    opened = []
    real_init = zipfile.ZipFile.__init__

    def counting(self, file, *a, **kw):
        opened.append(file)
        real_init(self, file, *a, **kw)

    monkeypatch.setattr(zipfile.ZipFile, "__init__", counting)
    ok = make_zip(tmp_path / "ok.zip", {"a.txt": b"a"})
    opened.clear()
    extract_zip(ok, work / "ok")
    assert len(opened) == 1


def test_swap_after_inspect_cannot_bypass_checks(tmp_path):
    z = make_zip(tmp_path / "a.zip", {"readme.txt": b"hi"})
    assert inspect_zip(z).ok
    make_zip(z, {"x": b"\0" * (5 * 1024 * 1024)})  # swapped between inspect and extract
    with pytest.raises(ArchiveError, match="zip bomb"):
        extract_zip(z, tmp_path / "out")
    assert not (tmp_path / "out").exists()
