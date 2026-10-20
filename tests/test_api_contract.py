"""Contract tests for the HTTP API used by the Android app."""
import http.client
import json
import os
import threading
from contextlib import contextmanager

import api_server


@contextmanager
def api(tmp_path, status=None, active=(), pause_callback=None):
    calls = []
    ctx = {
        "status": lambda: status or {"server_time": 1, "started_at": 1, "channels": [{"name": "alpha", "url": "https://www.youtube.com/@alpha", "quality": "480p", "status": "idle", "title": "", "last_error": "", "video_id": ""}]},
        "logs": lambda n: ["one", "two"][-n:],
        "active_prefixes": lambda: list(active),
        "log": calls.append,
        "add_channel": lambda n, u, q: (True, None),
        "remove_channel": lambda n: (True, None),
        "pause_channel": pause_callback or (lambda n: n == "alpha"),
        "resume_channel": lambda n: n == "alpha",
    }
    server = api_server.start_api({"api_token": "test-token", "output_dir": str(tmp_path), "api_bind": "127.0.0.1", "api_port": 0}, ctx)
    try:
        yield server, calls
    finally:
        server.shutdown()
        server.server_close()


def request(server, method, path, body=None, token="test-token"):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1])
    headers = {"Authorization": "Bearer " + token}
    if body is not None:
        headers["Content-Type"] = "application/json"
    conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
    response = conn.getresponse()
    payload = response.read()
    conn.close()
    return response.status, dict(response.getheaders()), payload


def test_existing_api_response_shapes_and_status_codes(tmp_path):
    (tmp_path / "old.mp4").write_bytes(b"video")
    with api(tmp_path) as (server, _):
        status, _, data = request(server, "GET", "/status")
        decoded = json.loads(data)
        assert status == 200
        assert {"server_time", "started_at", "channels", "disk"} <= decoded.keys()
        assert {"name", "url", "quality", "status", "title", "last_error", "video_id"} <= decoded["channels"][0].keys()

        status, _, data = request(server, "GET", "/recordings")
        assert status == 200
        assert {"recordings"} == json.loads(data).keys()
        assert {"name", "size", "modified", "in_progress"} == json.loads(data)["recordings"][0].keys()

        status, _, data = request(server, "GET", "/logs?limit=1")
        assert status == 200 and json.loads(data) == {"lines": ["two"]}
        status, _, data = request(server, "POST", "/channels", {"name": "new", "url": "https://www.youtube.com/@new", "quality": "480p"})
        assert (status, json.loads(data)) == (201, {"added": "new"})
        status, _, data = request(server, "POST", "/channels/alpha/stop")
        assert (status, json.loads(data)) == (200, {"channel": "alpha", "action": "stop"})
        status, _, data = request(server, "DELETE", "/channels/alpha")
        assert (status, json.loads(data)) == (200, {"removed": "alpha"})


def test_auth_and_recording_status_codes(tmp_path):
    (tmp_path / "active.mp4").write_bytes(b"x")
    with api(tmp_path, active=("active",)) as (server, _):
        status, _, data = request(server, "GET", "/status", token="wrong")
        assert (status, json.loads(data)) == (401, {"error": "unauthorized"})
        status, _, data = request(server, "GET", "/recordings/active.mp4")
        assert (status, json.loads(data)) == (409, {"error": "recording in progress"})
        status, _, data = request(server, "DELETE", "/recordings/missing.mp4")
        assert (status, json.loads(data)) == (404, {"error": "not found"})


def test_stop_api_returns_while_recording_worker_is_still_running(tmp_path):
    release = threading.Event()
    stop_requested = threading.Event()
    worker = threading.Thread(target=release.wait)
    worker.start()
    def request_stop(name):
        stop_requested.set()
        return name == "alpha"
    try:
        with api(tmp_path, pause_callback=request_stop) as (server, _):
            status, _, payload = request(server, "POST", "/channels/alpha/stop")
            assert (status, json.loads(payload)) == (200, {"channel": "alpha", "action": "stop"})
            assert stop_requested.is_set()
            assert worker.is_alive()
    finally:
        release.set()
        worker.join(timeout=1)


def test_api_rejects_invalid_names_urls_and_file_escapes(tmp_path):
    outside = tmp_path.parent / "outside.mp4"
    outside.write_bytes(b"secret")
    os.symlink(outside, tmp_path / "link.mp4")
    with api(tmp_path) as (server, _):
        for name in ("", "a" * 65, "bad/name"):
            status, _, _ = request(server, "POST", "/channels", {"name": name, "url": "https://youtube.com/@x", "quality": "480p"})
            assert status == 400
        for url in ("http://youtube.com/@x", "https://evil.example/@x"):
            status, _, _ = request(server, "POST", "/channels", {"name": "valid", "url": url, "quality": "480p"})
            assert status == 400
        for path in ("/recordings/../outside.mp4", "/recordings/link.mp4"):
            status, _, data = request(server, "GET", path)
            assert (status, json.loads(data)) == (404, {"error": "not found"})
