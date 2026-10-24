"""The isolated parser worker: which program hosts it, and what happens when it can't start."""

import sys
from concurrent.futures.process import BrokenProcessPool

from lazyfold import extract


class _Fut:
    def __init__(self, fn, args, fail):
        self.fn, self.args, self.fail = fn, args, fail

    def result(self, timeout=None):
        if self.fail:
            raise BrokenProcessPool("worker died")
        return self.fn(*self.args)


class _Pool:
    """Stands in for the process pool: runs inline, or fails like a worker that never started."""

    def __init__(self, fail):
        self.fail = fail

    def submit(self, fn, *args):
        return _Fut(fn, args, self.fail)

    def shutdown(self, **kw):
        pass


def _docx(tmp_path):
    import zipfile
    p = tmp_path / "file (1).docx"
    body = ('<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
            '<w:p><w:r><w:t>Offer letter from Fabrikam</w:t></w:r></w:p></w:body></w:document>')
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("word/document.xml", body)
    return p


def test_worker_executable_is_the_console_program_in_a_frozen_mac_app(tmp_path, monkeypatch):
    macos = tmp_path / "Lazyfold.app" / "Contents" / "MacOS"
    macos.mkdir(parents=True)
    (macos / "Lazyfold").write_bytes(b"")
    (macos / extract.CLI_EXE_NAME).write_bytes(b"")
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(macos / "Lazyfold"))
    assert extract.worker_executable() == str(macos / extract.CLI_EXE_NAME)


def test_worker_executable_default_elsewhere(monkeypatch):
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    assert extract.worker_executable() is None  # source run
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    assert extract.worker_executable() is None  # Windows: the window exe hosts it fine


def test_worker_that_never_starts_falls_back_and_says_so(tmp_path, monkeypatch):
    p = _docx(tmp_path)
    ex = extract.IsolatedExtractor()
    monkeypatch.setattr(ex, "_get_pool", lambda: _Pool(fail=True))
    text, _, warns = ex(p, "docx", ".docx")
    assert "Offer letter" in text and extract.IN_PROCESS_NOTE in warns
    text, _, warns = ex(p, "docx", ".docx")  # stays in-process for the rest of the scan
    assert "Offer letter" in text and extract.IN_PROCESS_NOTE in warns


def test_crash_after_the_worker_worked_is_not_retried_in_process(tmp_path, monkeypatch):
    p = _docx(tmp_path)
    ex = extract.IsolatedExtractor()
    monkeypatch.setattr(ex, "_get_pool", lambda: _Pool(fail=False))
    assert "Offer letter" in ex(p, "docx", ".docx")[0]
    monkeypatch.setattr(ex, "_get_pool", lambda: _Pool(fail=True))  # this file killed the worker
    text, _, warns = ex(p, "docx", ".docx")
    assert text == "" and "crashed" in warns[0]  # a hostile file never gets parsed in our process
