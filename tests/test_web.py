"""Local web API: access control and the full preview -> approve -> undo flow."""

import http.client
import json
import threading
import time
from http.server import ThreadingHTTPServer

import pytest

from conftest import snapshot
from lazyfold.web import App, make_handler


@pytest.fixture
def server(demo):
    root, _ = demo
    app = App(str(root))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    port = httpd.server_address[1]
    app.allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield app, port, root
    httpd.shutdown()
    httpd.server_close()


def call(port, method, path, body=None, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    data = json.dumps(body).encode() if body is not None else None
    h = {"Content-Type": "application/json"} if body is not None else {}
    h.update(headers or {})
    c.request(method, path, body=data, headers=h)
    r = c.getresponse()
    raw = r.read()
    c.close()
    try:
        return r.status, json.loads(raw)
    except json.JSONDecodeError:
        return r.status, raw.decode("utf-8", "replace")


def test_page_requires_token(server):
    app, port, _ = server
    assert call(port, "GET", "/")[0] == 403
    status, html = call(port, "GET", f"/?t={app.token}")
    assert status == 200 and "Lazyfold" in html


def test_api_requires_token(server):
    _, port, _ = server
    assert call(port, "GET", "/api/state")[0] == 403
    assert call(port, "POST", "/api/plan", {"folder": "x"})[0] == 403


def test_dns_rebinding_host_is_rejected(server):
    app, port, _ = server
    status, _ = call(port, "GET", "/api/state", headers={"X-Lazyfold-Token": app.token, "Host": "evil.example:80"})
    assert status == 403


def test_cross_origin_post_is_rejected(server):
    app, port, root = server
    status, _ = call(port, "POST", "/api/plan", {"folder": str(root)},
                     headers={"X-Lazyfold-Token": app.token, "Origin": "http://evil.example"})
    assert status == 403


def test_non_json_post_is_rejected(server):
    app, port, _ = server
    c = http.client.HTTPConnection("127.0.0.1", port)
    c.request("POST", "/api/undo", body="run_id=x", headers={"X-Lazyfold-Token": app.token,
                                                           "Content-Type": "application/x-www-form-urlencoded"})
    assert c.getresponse().status == 415


def test_full_flow_preview_approve_undo(server):
    app, port, root = server
    auth = {"X-Lazyfold-Token": app.token}
    before = snapshot(root)
    status, r = call(port, "POST", "/api/plan", {"folder": str(root), "use_model": False}, auth)
    assert status == 200
    for _ in range(100):
        status, job = call(port, "GET", f"/api/job/{r['job']}", headers=auth)
        if job["state"] != "running":
            break
        time.sleep(0.1)
    assert job["state"] == "done", job
    plan = job["plan"]
    assert snapshot(root) == before  # preview changed nothing

    moves = [p for p in plan["proposals"] if p["action"] == "move"]
    approved = [p["rel_path"] for p in moves][:10]
    renamed = next(p["rel_path"] for p in moves if p["rel_path"] in approved)
    status, rep = call(port, "POST", "/api/apply", {"plan_id": plan["plan_id"], "approved": approved,
                                                    "renames": {renamed: "..\\..\\My chosen name"}}, auth)
    assert status == 200 and rep["summary"]["moved"] == 10
    # The user-supplied name was sanitised and stayed inside its category folder.
    assert all(".." not in r["dst"] for r in rep["results"])
    assert len(set(snapshot(root)) - set(before)) == 10

    status, undo = call(port, "POST", "/api/undo", {"run_id": rep["run_id"]}, auth)
    assert status == 200 and undo["summary"]["restored"] == 10
    assert snapshot(root) == before


def test_bad_folder_gives_readable_error(server):
    app, port, _ = server
    status, r = call(port, "POST", "/api/plan", {"folder": "Z:\\does\\not\\exist"}, {"X-Lazyfold-Token": app.token})
    assert status == 400 and "folder" in r["error"].lower()


def test_settings_save_roundtrip(server):
    app, port, _ = server
    auth = {"X-Lazyfold-Token": app.token}
    status, r = call(port, "POST", "/api/settings", {"changes": {"recycle_old_installers_days": 0, "ocr": False}}, auth)
    assert status == 200 and r["settings"]["recycle_old_installers_days"] == 0 and r["settings"]["ocr"] is False
    status, r = call(port, "GET", "/api/settings", headers=auth)
    assert status == 200 and r["settings"]["ocr"] is False


def test_predictor_endpoints(server, monkeypatch):
    from lazyfold import predict
    app, port, _ = server
    auth = {"X-Lazyfold-Token": app.token}
    assert call(port, "GET", "/api/predictor")[0] == 403
    status, r = call(port, "GET", "/api/predictor", headers=auth)
    assert status == 200 and r["history"]["rows"] == 0 and isinstance(r["available"], bool)
    monkeypatch.setattr(predict, "selftest", lambda: {"ok": True, "auc": 0.95})
    status, r = call(port, "POST", "/api/predictor/selftest", {}, auth)
    assert status == 200 and r["job"]
    for _ in range(100):
        _, job = call(port, "GET", f"/api/job/{r['job']}", headers=auth)
        if job["state"] != "running":
            break
        time.sleep(0.05)
    assert job == {"state": "done", "result": {"ok": True, "auc": 0.95}, "error": None}
