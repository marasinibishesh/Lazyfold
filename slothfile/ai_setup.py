"""One-click setup of the optional local model (Ollama + Gemma).

The installed app has no terminal, so "run `ollama pull`" is not an answer for
its users. This module finds Ollama, installs it with winget only when asked,
starts its server hidden and downloads the model with a progress callback.

Everything stays on this PC: the server is started bound to loopback and the
only network traffic is Ollama's own download of the program and the model.
Every wait has a timeout, so a stuck download or server can never hang the UI.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable

from .llm import DEFAULT_MODEL, DEFAULT_URL

Progress = Callable[[str, "float | None"], None]

DOWNLOAD_HINT = "about 4.3 GB"  # size of DEFAULT_MODEL; shown before the user commits to a download
OLLAMA_DOWNLOAD_URL = "https://ollama.com/download"
OLLAMA_MAC_DOWNLOAD_URL = "https://ollama.com/download/mac"
MAC_OLLAMA_PATHS = ("/Applications/Ollama.app/Contents/Resources/ollama", "/usr/local/bin/ollama",
                    "/opt/homebrew/bin/ollama")
MAC_BREW_PATHS = ("/opt/homebrew/bin/brew", "/usr/local/bin/brew")
BREW_TIMEOUT = 30 * 60
WINGET_ARGS = ["install", "-e", "--id", "Ollama.Ollama", "--source", "winget", "--silent",
               "--accept-package-agreements", "--accept-source-agreements", "--disable-interactivity"]
WINGET_TIMEOUT = 30 * 60
# winget's "already installed, no newer version" code (APPINSTALLER_CLI_ERROR_UPDATE_NOT_APPLICABLE).
_WINGET_ALREADY = {0x8A15002B, 0x8A15002B - (1 << 32)}
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
_DETACHED = 0x00000008 if os.name == "nt" else 0  # DETACHED_PROCESS: survives the app closing
_MIN_TOTAL_FOR_PERCENT = 1 << 20  # tiny config layers alone would show a misleading 100%


def _noop(stage: str, percent: float | None) -> None:
    pass


def _is_mac() -> bool:
    return sys.platform == "darwin"


def _candidates() -> list[str]:
    """Where Ollama may live, in priority order (the user's explicit choice first)."""
    out: list[str] = []
    try:
        from .settings import load_settings
        configured = load_settings().get("ollama_path")
    except Exception:  # unreadable settings must not break detection
        configured = None
    if configured:
        out.append(configured)
    if _is_mac():
        # An app opened from Finder gets a minimal PATH, so the usual places are listed explicitly.
        out += [*MAC_OLLAMA_PATHS, os.path.expanduser("~/Applications/Ollama.app/Contents/Resources/ollama")]
        found = shutil.which("ollama")
        if found:
            out.append(found)
        return out
    found = shutil.which("ollama")
    if found:
        out.append(found)
    local = os.environ.get("LOCALAPPDATA")
    if local:
        out.append(os.path.join(local, "Programs", "Ollama", "ollama.exe"))  # official/winget per-user install
        out.append(os.path.join(local, "SlothFile-tools", "ollama", "ollama.exe"))
    return out


def find_ollama() -> str | None:
    for p in _candidates():
        if os.path.isfile(p):
            return os.path.abspath(p)
    return None


def _tags(base_url: str | None = None, timeout: float = 2.0) -> list[str] | None:
    """Installed model names, or None if no server answers."""
    try:
        with urllib.request.urlopen((base_url or DEFAULT_URL).rstrip("/") + "/api/tags", timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException):
        return None
    models = data.get("models") if isinstance(data, dict) else None
    names: list[str] = []
    for m in models if isinstance(models, list) else []:
        if isinstance(m, dict):
            names += [v for v in (m.get("name"), m.get("model")) if isinstance(v, str)]
    return names


def server_running(base_url: str | None = None) -> bool:
    return _tags(base_url) is not None


def _has_model(names: list[str], model: str) -> bool:
    return model in names or f"{model}:latest" in names


def status() -> dict[str, Any]:
    path = find_ollama()
    names = _tags()
    running = names is not None
    has_model = running and _has_model(names or [], DEFAULT_MODEL)
    if running and has_model:
        msg = f"Gemma ({DEFAULT_MODEL}) is ready. Documents are read on this PC only."
    elif running:
        msg = f"Ollama is running, but the Gemma model is not downloaded yet ({DOWNLOAD_HINT})."
    elif path:
        msg = "Ollama is installed but not running. Setup will start it."
    elif _is_mac():
        msg = (f"Ollama is not installed. Setup will install it with Homebrew and download Gemma ({DOWNLOAD_HINT})."
               if find_brew() else
               f"Ollama is not installed. Install it from {OLLAMA_MAC_DOWNLOAD_URL}, then run setup to "
               f"download Gemma ({DOWNLOAD_HINT}).")
    else:
        msg = (f"Ollama is not installed. Setup will install it with Windows Package Manager "
               f"and download Gemma ({DOWNLOAD_HINT}).")
    return {"ollama_installed": path is not None or running, "ollama_path": path, "server_running": running,
            "model_installed": has_model, "model": DEFAULT_MODEL, "ready": bool(running and has_model),
            "message": msg}


def start_server(path: str | None, wait: float = 20.0) -> bool:
    """Start `ollama serve` hidden and detached; True once /api/tags answers."""
    if server_running():
        return True
    if not path or not os.path.isfile(path):
        return False
    env = {**os.environ, "OLLAMA_HOST": "127.0.0.1:11434"}  # loopback only, whatever the user's env says
    # Windows: hidden and detached. Elsewhere: its own session, so it outlives the app too.
    detach = {"creationflags": _NO_WINDOW | _DETACHED} if sys.platform == "win32" else {"start_new_session": True}
    try:
        proc = subprocess.Popen([path, "serve"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, env=env, cwd=os.path.dirname(path),
                                close_fds=True, **detach)
    except OSError:
        return False
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        # Short probe: Windows takes ~2 s to refuse a loopback connect, which would make polling coarse.
        if _tags(timeout=1.0) is not None:
            return True
        if proc.poll() is not None:  # exited (port taken, broken install): no point waiting
            return server_running()
        time.sleep(0.25)
    return False


def install_ollama(progress: Progress | None = None) -> str:
    """Install Ollama per-user with winget. Only ever called on an explicit
    user request. Returns the detected ollama.exe path. macOS: Homebrew
    (`brew install ollama`) when it is installed, never a downloaded script."""
    report = progress or _noop
    if _is_mac():
        return _install_ollama_mac(report)
    winget = shutil.which("winget")
    if not winget:
        raise RuntimeError("Windows Package Manager (winget) is not available on this PC. "
                           f"Install Ollama from {OLLAMA_DOWNLOAD_URL}, then try again.")
    report("Installing Ollama (this can take a few minutes)", None)
    try:
        proc = subprocess.Popen([winget, *WINGET_ARGS], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, creationflags=_NO_WINDOW)
    except OSError as e:
        raise RuntimeError(f"Could not run winget ({e}). Install Ollama from {OLLAMA_DOWNLOAD_URL}.") from e
    tail: list[str] = []

    def pump() -> None:
        # winget draws progress with \r, so read raw chunks rather than lines.
        assert proc.stdout is not None
        while True:
            chunk = proc.stdout.read1(4096) if hasattr(proc.stdout, "read1") else proc.stdout.read(4096)
            if not chunk:
                return
            text = chunk.decode("utf-8", "replace")
            tail.append(text)
            del tail[:-20]
            pcts = re.findall(r"(\d{1,3})\s*%", text)
            if pcts and 0 <= int(pcts[-1]) <= 100:
                report("Downloading Ollama", float(pcts[-1]))

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    try:
        code = proc.wait(timeout=WINGET_TIMEOUT)
    except subprocess.TimeoutExpired:
        proc.kill()
        raise RuntimeError(f"Installing Ollama took too long. Install it from {OLLAMA_DOWNLOAD_URL}.") from None
    reader.join(timeout=5)
    if code != 0 and code not in _WINGET_ALREADY:
        last = " ".join("".join(tail).split())[-200:]
        raise RuntimeError(f"winget could not install Ollama (exit code {code:#x}"
                           f"{': ' + last if last else ''}). Install it from {OLLAMA_DOWNLOAD_URL}.")
    path = find_ollama()
    if not path:
        raise RuntimeError(f"Ollama was installed but SlothFile cannot find ollama.exe. "
                           f"Restart SlothFile, or install it from {OLLAMA_DOWNLOAD_URL}.")
    report("Ollama installed", 100.0)
    return path


def find_brew() -> str | None:
    """Homebrew's brew, if installed (an app opened from Finder has a minimal PATH)."""
    for p in (shutil.which("brew"), *MAC_BREW_PATHS):
        if p and os.path.isfile(p):
            return os.path.abspath(p)
    return None


def _install_ollama_mac(report: Progress) -> str:
    brew = find_brew()
    if not brew:
        raise RuntimeError(f"Homebrew is not installed on this Mac. Download Ollama from {OLLAMA_MAC_DOWNLOAD_URL}, "
                           "open it once, then try again.")
    report("Installing Ollama with Homebrew (this can take a few minutes)", None)
    env = {**os.environ, "HOMEBREW_NO_ENV_HINTS": "1"}
    try:
        r = subprocess.run([brew, "install", "ollama"], stdin=subprocess.DEVNULL, capture_output=True, text=True,
                           errors="replace", timeout=BREW_TIMEOUT, env=env)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"Installing Ollama took too long. Install it from {OLLAMA_MAC_DOWNLOAD_URL}.") from None
    except OSError as e:
        raise RuntimeError(f"Could not run Homebrew ({e}). Install Ollama from {OLLAMA_MAC_DOWNLOAD_URL}.") from e
    if r.returncode != 0:
        last = " ".join((r.stderr or r.stdout or "").split())[-200:]
        raise RuntimeError(f"Homebrew could not install Ollama (exit code {r.returncode}"
                           f"{': ' + last if last else ''}). Install it from {OLLAMA_MAC_DOWNLOAD_URL}.")
    path = find_ollama()
    if not path:
        raise RuntimeError(f"Ollama was installed but SlothFile cannot find it. "
                           f"Restart SlothFile, or install it from {OLLAMA_MAC_DOWNLOAD_URL}.")
    report("Ollama installed", 100.0)
    return path


def _gb(n: float) -> str:
    return f"{n / 1e9:.1f}"


def pull_model(progress: Progress | None = None, *, model: str = DEFAULT_MODEL, base_url: str | None = None,
               read_timeout: float = 120.0) -> None:
    """Download `model` through the local Ollama server, reporting overall
    percent across all layers. Each socket read times out after `read_timeout`."""
    report = progress or _noop
    report("Starting the Gemma download", None)
    req = urllib.request.Request((base_url or DEFAULT_URL).rstrip("/") + "/api/pull",
                                 data=json.dumps({"model": model, "stream": True}).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    layers: dict[str, tuple[int, int]] = {}
    shown = 0.0
    try:
        with urllib.request.urlopen(req, timeout=read_timeout) as resp:
            for raw in resp:
                line = raw.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    continue
                if not isinstance(ev, dict):
                    continue
                if ev.get("error"):
                    raise RuntimeError(f"Downloading Gemma failed: {str(ev['error'])[:200]}")
                st = str(ev.get("status") or "")
                digest, total, done = ev.get("digest"), ev.get("total"), ev.get("completed", 0)
                if isinstance(digest, str) and isinstance(total, int) and total > 0:
                    done = done if isinstance(done, int) else 0
                    layers[digest] = (min(max(done, 0), total), total)
                    all_done = sum(d for d, _ in layers.values())
                    all_total = sum(t for _, t in layers.values())
                    if all_total >= _MIN_TOTAL_FOR_PERCENT:
                        shown = max(shown, all_done * 100.0 / all_total)  # never jump backwards
                        report(f"Downloading Gemma ({_gb(all_done)} of {_gb(all_total)} GB)", round(shown, 1))
                    else:
                        report("Downloading Gemma", None)
                elif st == "success":
                    report("Gemma downloaded", 100.0)
                    return
                elif st:
                    report(st[:1].upper() + st[1:80], round(shown, 1) if shown else None)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            body = json.loads(body).get("error", body)
        except (ValueError, AttributeError):
            pass
        raise RuntimeError(f"Downloading Gemma failed (server error {e.code}: {str(body)[:200]})") from e
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError, http.client.HTTPException) as e:
        reason = getattr(e, "reason", e)
        raise RuntimeError(f"Downloading Gemma stopped ({reason}). Check your connection and try again; "
                           "the download resumes where it left off.") from e
    raise RuntimeError("Downloading Gemma ended before it finished. Try again; it resumes where it left off.")


def setup(progress: Progress | None = None) -> dict[str, Any]:
    """Detect -> install if missing -> start server -> pull if missing.
    Idempotent: when everything is ready it only checks and returns."""
    report = progress or _noop
    report("Checking for Ollama", None)
    path = find_ollama()
    names = _tags()
    if names is not None and _has_model(names, DEFAULT_MODEL):
        report("Gemma is ready", 100.0)
        return status()
    if names is None:
        if not path:
            path = install_ollama(report)
        report("Starting Ollama", None)
        if not start_server(path):
            raise RuntimeError("Ollama is installed but its server did not start. "
                               + ("Open Ollama from Applications, then try again." if _is_mac() else
                                  "Restart your PC or start Ollama from the Start menu, then try again."))
        names = _tags() or []
    if not _has_model(names, DEFAULT_MODEL):
        pull_model(report)
    result = status()
    if not result["ready"]:
        raise RuntimeError(result["message"])
    report("Gemma is ready", 100.0)
    return result
