"""One-click model setup: detection, winget install, server start, pull
progress and the web endpoints. Everything external is faked: no winget, no
real Ollama, no downloads."""

import io
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from slothfile import ai_setup, engine
from slothfile.llm import DEFAULT_MODEL
from slothfile.settings import save_settings

REAL_AUTOSTART = engine._autostart_ollama  # conftest replaces it per test; keep the real one


class FakeOllama:
    """Tiny stand-in for the Ollama REST API."""

    def __init__(self):
        self.up = True
        self.models: list[str] = []
        self.pull_lines: list[bytes] = []
        self.pull_requests: list[dict] = []
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _reply(self, code, body: bytes):
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if not fake.up:
                    return self._reply(503, b"{}")
                if self.path == "/api/tags":
                    models = [{"name": m, "model": m} for m in fake.models]
                    return self._reply(200, json.dumps({"models": models}).encode())
                self._reply(404, b"{}")

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                if not fake.up:
                    return self._reply(503, b"{}")
                if self.path == "/api/pull":
                    fake.pull_requests.append(body)
                    data = b"".join(line + b"\n" for line in fake.pull_lines)
                    return self._reply(200, data)
                if self.path == "/api/generate":
                    return self._reply(200, b'{"response": ""}')
                self._reply(404, b"{}")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def ollama(monkeypatch):
    fake = FakeOllama()
    monkeypatch.setattr(ai_setup, "DEFAULT_URL", fake.url)
    yield fake
    fake.close()


@pytest.fixture
def no_ollama(monkeypatch, tmp_path):
    """No Ollama anywhere: empty LOCALAPPDATA, nothing on PATH, nothing listening."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setattr(ai_setup.shutil, "which", lambda name: None)
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    monkeypatch.setattr(ai_setup, "DEFAULT_URL", f"http://127.0.0.1:{port}")
    return tmp_path / "local"


def touch(p):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"MZ")
    return p


def line(**kw) -> bytes:
    return json.dumps(kw).encode()


# ---------- detection ----------

def test_detection_order(no_ollama, monkeypatch, tmp_path):
    configured = touch(tmp_path / "custom" / "ollama.exe")
    on_path = touch(tmp_path / "path" / "ollama.exe")
    programs = touch(no_ollama / "Programs" / "Ollama" / "ollama.exe")
    tools = touch(no_ollama / "SlothFile-tools" / "ollama" / "ollama.exe")
    monkeypatch.setattr(ai_setup.shutil, "which", lambda name: str(on_path) if name == "ollama" else None)
    save_settings({"ollama_path": str(configured)})
    assert ai_setup.find_ollama() == str(configured)
    configured.unlink()  # configured but missing: fall through, don't fail
    assert ai_setup.find_ollama() == str(on_path)
    monkeypatch.setattr(ai_setup.shutil, "which", lambda name: None)
    assert ai_setup.find_ollama() == str(programs)
    programs.unlink()
    assert ai_setup.find_ollama() == str(tools)
    tools.unlink()
    assert ai_setup.find_ollama() is None


def test_status_when_nothing_installed(no_ollama):
    st = ai_setup.status()
    assert st == {"ollama_installed": False, "ollama_path": None, "server_running": False,
                  "model_installed": False, "model": DEFAULT_MODEL, "ready": False, "message": st["message"]}
    assert "not installed" in st["message"]


def test_status_installed_not_running(no_ollama):
    exe = touch(no_ollama / "Programs" / "Ollama" / "ollama.exe")
    st = ai_setup.status()
    assert st["ollama_installed"] and st["ollama_path"] == str(exe) and not st["server_running"]
    assert not st["ready"] and "not running" in st["message"]


def test_status_ready_and_model_missing(ollama, monkeypatch):
    monkeypatch.setattr(ai_setup, "find_ollama", lambda: None)  # e.g. installed somewhere we don't know
    st = ai_setup.status()
    assert st["server_running"] and st["ollama_installed"] and not st["model_installed"] and not st["ready"]
    ollama.models = [DEFAULT_MODEL]
    st = ai_setup.status()
    assert st["ready"] and st["model_installed"]


# ---------- start_server ----------

class FakeProc:
    def __init__(self, exits=False):
        self.exits = exits

    def poll(self):
        return 1 if self.exits else None


def test_start_server_hidden_detached(monkeypatch, tmp_path):
    exe = touch(tmp_path / "ollama.exe")
    calls = []
    state = {"up": False}

    def fake_popen(args, **kw):
        calls.append((args, kw))
        state["up"] = True
        return FakeProc()

    monkeypatch.setattr(ai_setup, "server_running", lambda base_url=None: state["up"])
    monkeypatch.setattr(ai_setup, "_tags", lambda base_url=None, timeout=2.0: [] if state["up"] else None)
    monkeypatch.setattr(ai_setup.subprocess, "Popen", fake_popen)
    assert ai_setup.start_server(str(exe), wait=2)
    (args, kw), = calls
    assert args == [str(exe), "serve"] and "shell" not in kw
    assert kw["creationflags"] & 0x08000000 and kw["creationflags"] & 0x00000008
    assert kw["stdin"] is kw["stdout"] is kw["stderr"] is ai_setup.subprocess.DEVNULL
    assert kw["env"]["OLLAMA_HOST"] == "127.0.0.1:11434"
    assert ai_setup.start_server(str(exe)) and len(calls) == 1  # already running: no second process


def test_start_server_gives_up_when_process_exits(monkeypatch, tmp_path):
    exe = touch(tmp_path / "ollama.exe")
    monkeypatch.setattr(ai_setup, "server_running", lambda base_url=None: False)
    monkeypatch.setattr(ai_setup, "_tags", lambda base_url=None, timeout=2.0: None)
    monkeypatch.setattr(ai_setup.subprocess, "Popen", lambda *a, **k: FakeProc(exits=True))
    assert ai_setup.start_server(str(exe), wait=30) is False
    assert ai_setup.start_server(None) is False
    assert ai_setup.start_server(str(tmp_path / "missing.exe")) is False


# ---------- install_ollama ----------

class FakeWinget:
    def __init__(self, output: bytes, code: int):
        self.stdout = io.BufferedReader(io.BytesIO(output))
        self.code = code

    def wait(self, timeout=None):
        return self.code

    def kill(self):
        pass


def test_winget_missing_message(monkeypatch):
    monkeypatch.setattr(ai_setup.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError, match=r"https://ollama\.com/download"):
        ai_setup.install_ollama()


def test_winget_failure_is_readable(monkeypatch, no_ollama):
    monkeypatch.setattr(ai_setup.shutil, "which", lambda name: r"C:\fake\winget.exe" if name == "winget" else None)
    monkeypatch.setattr(ai_setup.subprocess, "Popen", lambda *a, **k: FakeWinget(b"No package found", 0x8A150014))
    with pytest.raises(RuntimeError) as ei:
        ai_setup.install_ollama()
    assert "No package found" in str(ei.value) and "ollama.com/download" in str(ei.value)


def test_winget_success_reports_progress_and_redetects(monkeypatch, no_ollama):
    seen, calls = [], []
    monkeypatch.setattr(ai_setup.shutil, "which", lambda name: r"C:\fake\winget.exe" if name == "winget" else None)

    def fake_popen(args, **kw):
        calls.append((args, kw))
        touch(no_ollama / "Programs" / "Ollama" / "ollama.exe")  # what the installer would do
        return FakeWinget(b"  Downloading\r  ##   12%\r  ######   45%\rSuccessfully installed", 0)

    monkeypatch.setattr(ai_setup.subprocess, "Popen", fake_popen)
    path = ai_setup.install_ollama(lambda s, p: seen.append((s, p)))
    assert path.endswith(r"Programs\Ollama\ollama.exe")
    (args, kw), = calls
    assert args[0] == r"C:\fake\winget.exe" and "shell" not in kw
    assert args[1:] == ai_setup.WINGET_ARGS
    for flag in ("--silent", "--accept-package-agreements", "--accept-source-agreements", "--disable-interactivity"):
        assert flag in args
    assert ("Downloading Ollama", 45.0) in seen and seen[-1] == ("Ollama installed", 100.0)


def test_winget_already_installed_counts_as_success(monkeypatch, no_ollama):
    monkeypatch.setattr(ai_setup.shutil, "which", lambda name: r"C:\fake\winget.exe" if name == "winget" else None)
    touch(no_ollama / "Programs" / "Ollama" / "ollama.exe")
    monkeypatch.setattr(ai_setup.subprocess, "Popen", lambda *a, **k: FakeWinget(b"No newer version", 0x8A15002B))
    assert ai_setup.install_ollama().endswith("ollama.exe")


# ---------- pull_model ----------

def test_pull_progress_across_layers(ollama):
    mb = 1_000_000
    ollama.pull_lines = [
        line(status="pulling manifest"),
        line(status="pulling aaa", digest="sha256:aaa", total=3 * mb, completed=0),
        line(status="pulling bbb", digest="sha256:bbb", total=1 * mb),
        line(status="pulling aaa", digest="sha256:aaa", total=3 * mb, completed=int(1.5 * mb)),
        b"not json",
        b"",
        line(status="pulling bbb", digest="sha256:bbb", total=1 * mb, completed=1 * mb),
        line(status="pulling aaa", digest="sha256:aaa", total=3 * mb, completed=3 * mb),
        line(status="verifying sha256 digest"),
        line(status="writing manifest"),
        line(status="success"),
    ]
    seen = []
    ai_setup.pull_model(lambda s, p: seen.append((s, p)))
    assert ollama.pull_requests == [{"model": DEFAULT_MODEL, "stream": True}]
    pcts = [p for s, p in seen if s.startswith("Downloading Gemma (")]
    assert pcts == [0.0, 0.0, 37.5, 62.5, 100.0]
    assert ("Verifying sha256 digest", 100.0) in seen
    assert seen[-1] == ("Gemma downloaded", 100.0)


def test_pull_percent_never_goes_backwards(ollama):
    gb = 1_000_000_000
    ollama.pull_lines = [
        line(digest="a", total=2 * gb, completed=gb),  # 50%
        line(digest="b", total=2 * gb, completed=0),  # honest sum would drop to 25%
        line(digest="b", total=2 * gb, completed=2 * gb),  # 75%
        line(status="success"),
    ]
    seen = []
    ai_setup.pull_model(lambda s, p: seen.append((s, p)))
    assert [p for s, p in seen if s.startswith("Downloading Gemma (")] == [50.0, 50.0, 75.0]
    assert ("Downloading Gemma (3.0 of 4.0 GB)", 75.0) in seen


def test_tiny_layers_alone_show_no_percent(ollama):
    ollama.pull_lines = [line(digest="cfg", total=500, completed=500), line(status="success")]
    seen = []
    ai_setup.pull_model(lambda s, p: seen.append((s, p)))
    assert ("Downloading Gemma", None) in seen


def test_pull_error_line_raises(ollama):
    ollama.pull_lines = [line(status="pulling manifest"), line(error="pull model manifest: file does not exist")]
    with pytest.raises(RuntimeError, match="file does not exist"):
        ai_setup.pull_model()


def test_pull_stream_ending_early_raises(ollama):
    ollama.pull_lines = [line(digest="a", total=10 * 2**20, completed=2**20)]
    with pytest.raises(RuntimeError, match="ended before it finished"):
        ai_setup.pull_model()


def test_pull_without_server_raises_readably(no_ollama):
    with pytest.raises(RuntimeError, match="Downloading Gemma stopped"):
        ai_setup.pull_model(read_timeout=2)


# ---------- setup ----------

def _forbid(name):
    def f(*a, **k):
        raise AssertionError(f"{name} must not be called")
    return f


def test_setup_is_idempotent_when_ready(ollama, monkeypatch):
    ollama.models = [DEFAULT_MODEL]
    for name in ("install_ollama", "start_server", "pull_model"):
        monkeypatch.setattr(ai_setup, name, _forbid(name))
    seen = []
    assert ai_setup.setup(lambda s, p: seen.append((s, p)))["ready"]
    assert ai_setup.setup()["ready"]
    assert seen[-1] == ("Gemma is ready", 100.0)


def test_setup_full_path(ollama, monkeypatch, tmp_path):
    ollama.up = False
    order = []
    exe = str(tmp_path / "ollama.exe")
    monkeypatch.setattr(ai_setup, "find_ollama", lambda: None if "install" not in order else exe)

    def install(progress):
        order.append("install")
        return exe

    def start(path, wait=20.0):
        order.append(("start", path))
        ollama.up = True
        return True

    def pull(progress):
        order.append("pull")
        ollama.models = [DEFAULT_MODEL]

    monkeypatch.setattr(ai_setup, "install_ollama", install)
    monkeypatch.setattr(ai_setup, "start_server", start)
    monkeypatch.setattr(ai_setup, "pull_model", pull)
    result = ai_setup.setup()
    assert order == ["install", ("start", exe), "pull"]
    assert result["ready"] and result["ollama_path"] == exe


def test_setup_skips_install_when_installed_and_reports_failed_start(ollama, monkeypatch, tmp_path):
    ollama.up = False
    monkeypatch.setattr(ai_setup, "find_ollama", lambda: str(tmp_path / "ollama.exe"))
    monkeypatch.setattr(ai_setup, "install_ollama", _forbid("install_ollama"))
    monkeypatch.setattr(ai_setup, "start_server", lambda path, wait=20.0: False)
    with pytest.raises(RuntimeError, match="did not start"):
        ai_setup.setup()


# ---------- make_classifier autostart ----------

def test_make_classifier_starts_stopped_ollama_once(ollama, monkeypatch):
    ollama.up = False
    ollama.models = [DEFAULT_MODEL]
    monkeypatch.setattr(engine, "DEFAULT_URL", ollama.url)
    calls = []

    def autostart():
        calls.append(1)
        ollama.up = True
        return True

    monkeypatch.setattr(engine, "_autostart_ollama", autostart)
    clf, label, _ = engine.make_classifier(DEFAULT_MODEL)
    assert label == "rules+model" and calls == [1]


def test_make_classifier_custom_url_never_autostarts(monkeypatch):
    monkeypatch.setattr(engine, "_autostart_ollama", _forbid("_autostart_ollama"))
    _, label, status = engine.make_classifier(DEFAULT_MODEL, base_url="http://127.0.0.1:9")
    assert label == "rules" and "unavailable" in status


def test_autostart_respects_settings_and_running_server(monkeypatch, tmp_path):
    started = []
    monkeypatch.setattr(ai_setup, "find_ollama", lambda: str(tmp_path / "ollama.exe"))
    monkeypatch.setattr(ai_setup, "server_running", lambda base_url=None: False)
    monkeypatch.setattr(ai_setup, "start_server", lambda path, wait=20.0: started.append(wait) or True)
    save_settings({"use_model": False})
    assert REAL_AUTOSTART() is False and started == []
    save_settings({"use_model": True})
    assert REAL_AUTOSTART() is True and started == [15.0]
    monkeypatch.setattr(ai_setup, "server_running", lambda base_url=None: True)
    assert REAL_AUTOSTART() is False and started == [15.0]
    monkeypatch.setattr(ai_setup, "find_ollama", lambda: None)
    monkeypatch.setattr(ai_setup, "server_running", lambda base_url=None: False)
    assert REAL_AUTOSTART() is False
    monkeypatch.setattr(ai_setup, "find_ollama", _forbid("find_ollama"))
    assert REAL_AUTOSTART() is False  # never raises


# ---------- web endpoints ----------

@pytest.fixture
def server(tmp_path):
    from slothfile.web import App, make_handler
    app = App(None)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    port = httpd.server_address[1]
    app.allowed_hosts = {f"127.0.0.1:{port}"}
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield app, port
    httpd.shutdown()
    httpd.server_close()


def call(app, port, method, path, body=None):
    import http.client
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    h = {"X-SlothFile-Token": app.token}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    c.request(method, path, body=data, headers=h)
    r = c.getresponse()
    out = r.status, json.loads(r.read())
    c.close()
    return out


def wait_job(app, port, job, until):
    import time
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        _, j = call(app, port, "GET", f"/api/job/{job}")
        if until(j):
            return j
        time.sleep(0.02)
    raise AssertionError("job did not reach the expected state")


def test_status_endpoint(server, monkeypatch):
    app, port = server
    monkeypatch.setattr(ai_setup, "status", lambda: {"ready": True, "message": "ok"})
    assert call(app, port, "GET", "/api/ai/status") == (200, {"ready": True, "message": "ok"})


def test_setup_endpoint_confirm_progress_and_409(server, monkeypatch):
    app, port = server
    gate = threading.Event()

    def fake_setup(progress):
        progress("Downloading Gemma (1.0 of 4.3 GB)", 23.3)
        assert gate.wait(10)
        return {"ready": True}

    monkeypatch.setattr(ai_setup, "setup", fake_setup)
    for body in ({}, {"confirm": "true"}, {"confirm": 1}):
        status, r = call(app, port, "POST", "/api/ai/setup", body)
        assert status == 400 and "confirm" in r["error"]
    status, r = call(app, port, "POST", "/api/ai/setup", {"confirm": True})
    assert status == 200
    job = r["job"]
    j = wait_job(app, port, job, lambda j: j.get("progress"))
    assert j["state"] == "running"
    assert j["progress"] == {"stage": "Downloading Gemma (1.0 of 4.3 GB)", "percent": 23.3}
    assert call(app, port, "POST", "/api/ai/setup", {"confirm": True})[0] == 409
    gate.set()
    j = wait_job(app, port, job, lambda j: j["state"] != "running")
    assert j["state"] == "done" and j["result"] == {"ready": True}
    status, r = call(app, port, "POST", "/api/ai/setup", {"confirm": True})  # lock released
    assert status == 200
    wait_job(app, port, r["job"], lambda j: j["state"] != "running")


def test_setup_endpoint_error_releases_lock(server, monkeypatch):
    app, port = server

    def boom(progress):
        raise RuntimeError("winget could not install Ollama")

    monkeypatch.setattr(ai_setup, "setup", boom)
    _, r = call(app, port, "POST", "/api/ai/setup", {"confirm": True})
    j = wait_job(app, port, r["job"], lambda j: j["state"] != "running")
    assert j["state"] == "error" and "winget" in j["error"] and j["progress"] is None
    assert not app.ai_lock.locked()


def test_other_jobs_have_no_progress_key(server, monkeypatch):
    app, port = server
    job = app.start_job(lambda: 42)
    j = wait_job(app, port, job, lambda j: j["state"] == "done")
    assert j == {"state": "done", "result": 42, "error": None}
