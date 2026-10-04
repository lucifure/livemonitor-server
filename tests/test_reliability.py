import json
import subprocess
import threading
from types import SimpleNamespace

import pytest
import sys


class FakeRequests:
    class exceptions:
        class ProxyError(Exception):
            pass
    class ConnectionError(Exception):
        pass

    def get(self, *args, **kwargs):
        raise AssertionError("requests.get must be mocked")

requests = FakeRequests()
sys.modules["requests"] = requests

import livemonitor_server as server


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    server.STATE.clear(); server.CONTROL.clear(); server.LOG_BUFFER.clear(); server.GLOBALS.clear()
    monkeypatch.setattr(server, "log", lambda name, message: server.LOG_BUFFER.append(message))


FIXTURES = {
    "live": '''<script>ytInitialPlayerResponse = {"videoDetails":{"videoId":"abc","title":"A live show","isLiveContent":true},"playabilityStatus":{"status":"OK"},"streamingData":{"hlsManifestUrl":"https://example/live"}};</script>''',
    "normal": '''<html><script>var ytInitialData = {}; var channelMetadata = {};</script></html>''',
    "blocked": '''<html>Unusual traffic. Please confirm you're not a bot.</html>''',
    "unexpected": '''<html><body>Something completely different</body></html>''',
}


def player_html(live=True):
    data = {"videoDetails": {"videoId": "abc", "title": "A live show", "isLiveContent": live}, "playabilityStatus": {"status": "OK"}, "streamingData": {"hlsManifestUrl": "https://example/live"}}
    return "<script>ytInitialPlayerResponse = " + json.dumps(data) + ";</script>"


def test_errors_keep_status_and_clear_on_next_success(monkeypatch):
    server.set_state("a", status="idle")
    monkeypatch.setattr(requests, "get", lambda *args, **kwargs: (_ for _ in ()).throw(requests.ConnectionError("down")))
    assert server.check_live("https://youtube.com/@a", "", "a") is None
    assert server.STATE["a"]["status"] == "idle"
    assert "request failed" in server.STATE["a"]["last_error"]
    monkeypatch.setattr(requests, "get", lambda *args, **kwargs: SimpleNamespace(status_code=200, text=player_html()))
    assert server.check_live("https://youtube.com/@a", "", "a")["video_id"] == "abc"
    assert server.STATE["a"]["last_error"] is None


def test_unknown_and_rate_limited_pages_are_errors_with_five_minute_backoff(monkeypatch):
    server.set_state("a", status="idle")
    monkeypatch.setattr(requests, "get", lambda *args, **kwargs: SimpleNamespace(status_code=200, text="consent page"))
    assert server.check_live("https://youtube.com/@a", "", "a") is None
    assert server.STATE["a"]["last_error"].startswith("unexpected page")
    assert server.STATE["a"]["backoff_seconds"] == 300
    monkeypatch.setattr(requests, "get", lambda *args, **kwargs: SimpleNamespace(status_code=429, text=""))
    server.check_live("https://youtube.com/@a", "", "a")
    assert server.STATE["a"]["backoff_seconds"] == 300


def test_proxy_failure_retries_once_directly(monkeypatch):
    seen = []
    def get(*args, **kwargs):
        seen.append("proxies" in kwargs)
        if "proxies" in kwargs:
            raise requests.exceptions.ProxyError("proxy unavailable")
        return SimpleNamespace(status_code=200, text=player_html())
    monkeypatch.setattr(requests, "get", get)
    assert server.check_live("https://youtube.com/@a", "http://proxy", "a")["video_id"] == "abc"
    assert seen == [True, False]
    assert any("succeeded directly" in x for x in server.LOG_BUFFER)


def test_paused_channel_is_saved_and_recreated_paused(tmp_path, monkeypatch):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"channels": [{"name": "a", "url": "https://youtube.com/@a"}]}))
    monkeypatch.setattr(server, "CONFIG_PATH", str(config))
    server.CONTROL["a"] = {"pause": threading.Event(), "remove": threading.Event()}
    assert server.pause_channel("a")
    assert json.loads(config.read_text())["channels"][0]["paused"] is True
    monkeypatch.setattr(server, "monitor_channel", lambda c: None)
    server.spawn_channel_thread(json.loads(config.read_text())["channels"][0])
    assert server.CONTROL["a"]["pause"].is_set()
    assert server.resume_channel("a")
    assert json.loads(config.read_text())["channels"][0]["paused"] is False


def test_recording_checks_disk_and_logs_ytdlp_failure_tail(tmp_path, monkeypatch):
    server.GLOBALS["min_free_gb"] = 3
    monkeypatch.setattr(server.shutil, "disk_usage", lambda p: SimpleNamespace(free=1))
    assert server.record_stream("a", "abc", "show", "", str(tmp_path), "480p") is False
    assert "not enough free disk" in server.STATE["a"]["last_error"]
    monkeypatch.setattr(server.shutil, "disk_usage", lambda p: SimpleNamespace(free=10**12))
    monkeypatch.setattr(server, "set_state", lambda *args, **kwargs: None)
    monkeypatch.setattr(server, "sanitize_filename", lambda title: "show")
    monkeypatch.setattr(server, "datetime", SimpleNamespace(now=lambda: SimpleNamespace(strftime=lambda fmt: "01012026" if "%d" in fmt else "00-00-00")))
    monkeypatch.setattr(server.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout="one\ntwo\nthree"))
    monkeypatch.setattr(server.os, "listdir", lambda p: [])
    assert server.record_stream("a", "abc", "show", "", str(tmp_path), "480p") is False
    assert any("yt-dlp failed; output tail: one\ntwo\nthree" in x for x in server.LOG_BUFFER)


def test_status_uses_empty_strings_and_includes_version():
    server.GLOBALS["version"] = "abc123"
    server.set_state("a", status="idle", title=None, last_error=None, video_id=None)
    status = server.get_status()
    assert status["version"] == "abc123"
    assert {k: status["channels"][0][k] for k in ("title", "last_error", "video_id")} == {"title": "", "last_error": "", "video_id": ""}


def test_server_version_has_safe_fallback(monkeypatch):
    monkeypatch.setattr(server.subprocess, "check_output", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("no git")))
    assert server.server_version() == "unknown"


def test_monitor_rechecks_and_starts_a_new_recording_after_one_ends(monkeypatch):
    channel = {"name": "a", "url": "https://youtube.com/@a", "quality": "480p"}
    server.GLOBALS.update(proxy="", output_dir="/tmp", poll_interval=1)
    server.CONTROL["a"] = {"pause": threading.Event(), "remove": threading.Event()}
    monkeypatch.setattr(server, "check_live", lambda *args: {"video_id": "abc", "title": "show"})
    starts = []
    def record(*args):
        starts.append(args)
        if len(starts) == 2:
            server.CONTROL["a"]["remove"].set()
        return True
    monkeypatch.setattr(server, "record_stream", record)
    delays = []
    monkeypatch.setattr(server.time, "sleep", lambda seconds: delays.append(seconds))
    server.monitor_channel(channel)
    assert len(starts) == 2
    assert len(delays) >= 5  # five one-second sleeps make the restart delay responsive to stop.


def test_main_logs_version_before_starting_workers(monkeypatch, tmp_path):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"channels": [], "api_token": "", "min_free_gb": 4}))
    monkeypatch.setattr(server, "CONFIG_PATH", str(config))
    monkeypatch.setattr(server, "server_version", lambda: "deadbee")
    monkeypatch.setattr(server.api_server, "start_api", lambda *args: None)
    monkeypatch.setattr(server.time, "sleep", lambda _: (_ for _ in ()).throw(RuntimeError("stop")))
    with pytest.raises(RuntimeError, match="stop"):
        server.main()
    assert any("version deadbee" in line for line in server.LOG_BUFFER)
    assert server.GLOBALS["min_free_gb"] == 4


def response(html, url="https://www.youtube.com/@a"):
    return SimpleNamespace(status_code=200, text=html, url=url)


@pytest.mark.parametrize("fixture, expected", [
    ("live", "live"),
    ("normal", "not_live"),
    ("blocked", "blocked"),
    ("unexpected", "unexpected"),
])
def test_page_classification_fixtures(fixture, expected):
    kind, info = server._classify_page(response(FIXTURES[fixture]))
    assert kind == expected
    if expected == "live":
        assert info["video_id"] == "abc"


def test_blocked_page_is_throttled_and_backs_off(monkeypatch):
    server.set_state("a", status="idle")
    monkeypatch.setattr(requests, "get", lambda *args, **kwargs: response(FIXTURES["blocked"]))
    assert server.check_live("https://youtube.com/@a", "", "a") is None
    assert server.STATE["a"]["last_error"] == "YouTube blocked this network (bot or consent check)"
    assert server.STATE["a"]["backoff_seconds"] == 600
    assert len(server.LOG_BUFFER) == 1
    server.check_live("https://youtube.com/@a", "", "a")
    assert len(server.LOG_BUFFER) == 1


def test_normal_page_clears_error_without_logging(monkeypatch):
    server.set_state("a", status="idle", last_error="old error")
    monkeypatch.setattr(requests, "get", lambda *args, **kwargs: response(FIXTURES["normal"]))
    assert server.check_live("https://youtube.com/@a", "", "a") is None
    assert server.STATE["a"]["last_error"] is None
    assert server.LOG_BUFFER == []


def test_yt_dlp_bot_output_sets_blocked_error_and_uses_five_line_tail(tmp_path, monkeypatch):
    server.GLOBALS["min_free_gb"] = 0
    monkeypatch.setattr(server.shutil, "disk_usage", lambda p: SimpleNamespace(free=10**12))
    monkeypatch.setattr(server, "datetime", SimpleNamespace(now=lambda: SimpleNamespace(strftime=lambda fmt: "01012026" if "%d" in fmt else "00-00-00")))
    lines = ["one", "two", "three", "four", "five", "Sign in to confirm", "seven"]
    monkeypatch.setattr(server.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout="\n".join(lines)))
    monkeypatch.setattr(server.os, "listdir", lambda p: [])
    assert server.record_stream("a", "abc", "show", "", str(tmp_path), "480p") is False
    assert server.STATE["a"]["last_error"] == "YouTube blocked this network (bot or consent check)"
    assert server.STATE["a"]["backoff_seconds"] == 600
    assert any("Sign in to confirm" in x and "seven" in x for x in server.LOG_BUFFER)
