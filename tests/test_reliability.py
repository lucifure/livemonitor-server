import json
import io
import signal
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


class FakeProcess:
    pid = 4321

    def __init__(self, returncode=0, output="", timeout_count=0):
        self.returncode = returncode
        self.stdout = io.StringIO(output)
        self.timeout_count = timeout_count
        self.wait_calls = 0

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.wait_calls += 1
        if timeout is not None and self.timeout_count:
            self.timeout_count -= 1
            raise subprocess.TimeoutExpired("yt-dlp", timeout)
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    server.STATE.clear(); server.CONTROL.clear(); server.LOG_BUFFER.clear(); server.GLOBALS.clear()
    monkeypatch.setattr(server, "log", lambda name, message: server.LOG_BUFFER.append(message))


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
    monkeypatch.setattr(server.subprocess, "Popen", lambda *args, **kwargs: FakeProcess(1, "one\ntwo\nthree\n"))
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
        server.set_state("a", last_recording_duration_seconds=120)
        if len(starts) == 2:
            server.CONTROL["a"]["remove"].set()
        return True
    monkeypatch.setattr(server, "record_stream", record)
    delays = []
    monkeypatch.setattr(server.time, "sleep", lambda seconds: delays.append(seconds))
    server.monitor_channel(channel)
    assert len(starts) == 2
    assert len(delays) >= 5  # five one-second sleeps make the restart delay responsive to stop.


def test_monitor_failure_backoff_grows_and_caps(monkeypatch):
    channel = {"name": "a", "url": "https://youtube.com/@a", "quality": "480p"}
    server.GLOBALS.update(proxy="", output_dir="/tmp", poll_interval=1)
    server.CONTROL["a"] = {"pause": threading.Event(), "remove": threading.Event()}
    monkeypatch.setattr(server, "check_live", lambda *args: {"video_id": "abc", "title": "show"})
    attempts = []
    def failed(*args):
        attempts.append(1)
        if len(attempts) == 8:
            server.CONTROL["a"]["remove"].set()
        server.set_state("a", last_recording_duration_seconds=3)
        return False
    monkeypatch.setattr(server, "record_stream", failed)
    monkeypatch.setattr(server.time, "sleep", lambda seconds: None)
    messages = []
    monkeypatch.setattr(server, "log", lambda name, message: messages.append(message))
    server.monitor_channel(channel)
    waits = [int(line.split("waiting ")[1].split("s")[0]) for line in messages if "attempt failed" in line]
    assert waits == [30, 60, 120, 240, 300, 300, 300, 300]
    assert not any("paused" in line for line in messages)


def test_long_success_resets_failure_backoff(monkeypatch):
    channel = {"name": "a", "url": "https://youtube.com/@a", "quality": "480p"}
    server.GLOBALS.update(proxy="", output_dir="/tmp", poll_interval=1)
    server.CONTROL["a"] = {"pause": threading.Event(), "remove": threading.Event()}
    monkeypatch.setattr(server, "check_live", lambda *args: {"video_id": "abc", "title": "show"})
    attempts = []
    def record(*args):
        attempts.append(True)
        if len(attempts) == 4:
            server.CONTROL["a"]["remove"].set()
        server.set_state("a", last_recording_duration_seconds=130 if len(attempts) == 3 else 3)
        return len(attempts) == 3
    monkeypatch.setattr(server, "record_stream", record)
    monkeypatch.setattr(server.time, "sleep", lambda seconds: None)
    messages = []
    monkeypatch.setattr(server, "log", lambda name, message: messages.append(message))
    server.monitor_channel(channel)
    waits = [int(line.split("waiting ")[1].split("s")[0]) for line in messages if "attempt failed" in line]
    assert waits == [30, 60, 30]


def test_empty_recording_leftovers_only_are_removed(tmp_path):
    (tmp_path / "base.f135.mp4").write_bytes(b"")
    (tmp_path / "base.f140.mp4").write_bytes(b"audio")
    (tmp_path / "other.f135.mp4").write_bytes(b"")
    server.cleanup_empty_recording_files(str(tmp_path), "base")
    assert not (tmp_path / "base.f135.mp4").exists()
    assert (tmp_path / "base.f140.mp4").read_bytes() == b"audio"
    assert (tmp_path / "other.f135.mp4").exists()


def test_ffmpeg_failure_keeps_ts_and_reports_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(server.shutil, "disk_usage", lambda p: SimpleNamespace(free=10**12))
    monkeypatch.setattr(server, "sanitize_filename", lambda title: "base")
    monkeypatch.setattr(server, "datetime", SimpleNamespace(now=lambda: SimpleNamespace(strftime=lambda fmt: "01012026" if "%d" in fmt else "00-00-00")))
    monkeypatch.setattr(server.subprocess, "Popen", lambda cmd, **kwargs: FakeProcess(0))
    monkeypatch.setattr(server.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=1))
    ts = tmp_path / "base_01012026_00-00-00.ts"
    def listdir(path):
        if not ts.exists():
            ts.write_bytes(b"video")
        return [ts.name]
    monkeypatch.setattr(server.os, "listdir", listdir)
    assert server.record_stream("a", "abc", "show", "", str(tmp_path), "480p") is False
    assert ts.read_bytes() == b"video"
    assert "keeping original .ts" in server.STATE["a"]["last_error"]


def test_ytdlp_command_omits_certificate_bypass(tmp_path, monkeypatch):
    monkeypatch.setattr(server.shutil, "disk_usage", lambda p: SimpleNamespace(free=10**12))
    monkeypatch.setattr(server, "sanitize_filename", lambda title: "base")
    monkeypatch.setattr(server, "datetime", SimpleNamespace(now=lambda: SimpleNamespace(strftime=lambda fmt: "01012026" if "%d" in fmt else "00-00-00")))
    commands = []
    monkeypatch.setattr(server.subprocess, "Popen", lambda cmd, **kwargs: (commands.append(cmd) or FakeProcess(1, "failed\n")))
    monkeypatch.setattr(server.os, "listdir", lambda p: [])
    assert server.record_stream("a", "abc", "show", "", str(tmp_path), "480p") is False
    assert "--no-check-certificates" not in commands[0]


def test_change_me_disables_api(capsys, tmp_path):
    assert server.api_server.start_api({"api_token": "CHANGE_ME", "output_dir": str(tmp_path)}, {}) is None
    assert "API disabled" in capsys.readouterr().out


def test_missing_proxy_config_defaults_to_direct(monkeypatch, tmp_path):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"channels": [], "api_token": "", "min_free_gb": 4}))
    monkeypatch.setattr(server, "CONFIG_PATH", str(config))
    monkeypatch.setattr(server, "server_version", lambda: "deadbee")
    monkeypatch.setattr(server.api_server, "start_api", lambda *args: None)
    monkeypatch.setattr(server.time, "sleep", lambda _: (_ for _ in ()).throw(RuntimeError("stop")))
    with pytest.raises(RuntimeError, match="stop"):
        server.main()
    assert server.GLOBALS["proxy"] == ""


def test_stop_sends_sigint_then_finalizes_recording(tmp_path, monkeypatch):
    server.GLOBALS["min_free_gb"] = 0
    stop = threading.Event()
    server.CONTROL["a"] = {"pause": threading.Event(), "remove": threading.Event(), "stop_now": stop}
    monkeypatch.setattr(server.shutil, "disk_usage", lambda p: SimpleNamespace(free=10**12))
    monkeypatch.setattr(server, "sanitize_filename", lambda title: "show")
    monkeypatch.setattr(server, "datetime", SimpleNamespace(now=lambda: SimpleNamespace(strftime=lambda fmt: "01012026" if "%d" in fmt else "00-00-00")))
    ts = tmp_path / "show_01012026_00-00-00.ts"
    process = FakeProcess(None)
    kwargs_seen = []
    def popen(cmd, **kwargs):
        kwargs_seen.append(kwargs)
        ts.write_bytes(b"recorded transport stream")
        return process
    monkeypatch.setattr(server.subprocess, "Popen", popen)
    def killpg(pid, sig):
        assert pid == process.pid
        assert sig == signal.SIGINT
        process.returncode = 130
    signals = []
    def record_signal(pid, sig):
        signals.append(sig)
        killpg(pid, sig)
    monkeypatch.setattr(server.os, "killpg", record_signal)
    def ffmpeg(cmd, **kwargs):
        (tmp_path / "show_01012026_00-00-00.mp4").write_bytes(b"saved mp4")
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(server.subprocess, "run", ffmpeg)
    transitions = []
    real_set_state = server.set_state
    def tracked_state(name, **values):
        if "status" in values:
            transitions.append(values["status"])
        real_set_state(name, **values)
    monkeypatch.setattr(server, "set_state", tracked_state)
    monkeypatch.setattr(server.time, "sleep", lambda _: stop.set())
    server.STATE["a"] = {"last_error": None}
    # Trigger the user action only after Popen has started.
    monkeypatch.setattr(server.time, "sleep", lambda _: (server.CONTROL["a"]["pause"].set(), stop.set()))
    assert server.record_stream("a", "abc", "show", "", str(tmp_path), "480p") is True
    assert signals == [signal.SIGINT]
    assert kwargs_seen[0]["start_new_session"] is True
    assert (tmp_path / "show_01012026_00-00-00.mp4").read_bytes() == b"saved mp4"
    assert not ts.exists()
    compact = [value for i, value in enumerate(transitions) if i == 0 or transitions[i - 1] != value]
    assert compact == ["recording", "finalizing", "paused"]
    assert server.STATE["a"].get("last_error") is None


def test_stop_escalates_to_sigterm_and_sigkill(tmp_path, monkeypatch):
    server.GLOBALS["min_free_gb"] = 0
    stop = threading.Event()
    server.CONTROL["a"] = {"pause": threading.Event(), "remove": threading.Event(), "stop_now": stop}
    monkeypatch.setattr(server.shutil, "disk_usage", lambda p: SimpleNamespace(free=10**12))
    monkeypatch.setattr(server, "sanitize_filename", lambda title: "show")
    monkeypatch.setattr(server, "datetime", SimpleNamespace(now=lambda: SimpleNamespace(strftime=lambda fmt: "01012026" if "%d" in fmt else "00-00-00")))
    process = FakeProcess(None, timeout_count=2)
    monkeypatch.setattr(server.subprocess, "Popen", lambda *args, **kwargs: process)
    signals = []
    def killpg(pid, sig):
        signals.append(sig)
        if sig == signal.SIGKILL:
            process.returncode = -9
    monkeypatch.setattr(server.os, "killpg", killpg)
    monkeypatch.setattr(server.time, "sleep", lambda _: (server.CONTROL["a"]["pause"].set(), stop.set()))
    monkeypatch.setattr(server, "STOP_GRACE_SECONDS", 0.01)
    monkeypatch.setattr(server, "STOP_TERM_SECONDS", 0.01)
    assert server.record_stream("a", "abc", "show", "", str(tmp_path), "480p") is True
    assert signals == [signal.SIGINT, signal.SIGTERM, signal.SIGKILL]
    assert server.STATE["a"]["status"] == "paused"
    assert server.STATE["a"].get("last_error") is None


def test_output_reader_keeps_only_last_200_lines():
    from collections import deque
    lines = deque(maxlen=200)
    server.read_process_output(io.StringIO("".join(f"line {i}\n" for i in range(250))), lines)
    assert len(lines) == 200
    assert lines[0] == "line 50"
    assert lines[-1] == "line 249"


def test_pause_returns_without_waiting_for_active_recording(tmp_path, monkeypatch):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"channels": [{"name": "a", "url": "https://youtube.com/@a"}]}))
    monkeypatch.setattr(server, "CONFIG_PATH", str(config))
    release = threading.Event()
    active = threading.Thread(target=release.wait)
    active.start()
    server.CONTROL["a"] = {"pause": threading.Event(), "remove": threading.Event(), "stop_now": threading.Event()}
    assert server.pause_channel("a") is True
    assert active.is_alive()
    assert server.CONTROL["a"]["stop_now"].is_set()
    assert json.loads(config.read_text())["channels"][0]["paused"] is True
    release.set()
    active.join(timeout=1)


def test_user_stop_does_not_count_toward_backoff(monkeypatch):
    channel = {"name": "a", "url": "https://youtube.com/@a", "quality": "480p"}
    server.GLOBALS.update(proxy="", output_dir="/tmp", poll_interval=1)
    server.CONTROL["a"] = {"pause": threading.Event(), "remove": threading.Event(), "stop_now": threading.Event()}
    monkeypatch.setattr(server, "check_live", lambda *args: {"video_id": "abc", "title": "show"})
    def stopped(*args):
        server.CONTROL["a"]["pause"].set()
        server.set_state("a", last_recording_duration_seconds=2, last_error=None, status="paused")
        return True
    monkeypatch.setattr(server, "record_stream", stopped)
    monkeypatch.setattr(server.time, "sleep", lambda _: server.CONTROL["a"]["remove"].set())
    messages = []
    monkeypatch.setattr(server, "log", lambda name, message: messages.append(message))
    server.monitor_channel(channel)
    assert not any("attempt failed" in line for line in messages)
    assert server.STATE == {}


def test_not_live_success_resets_failure_streak(monkeypatch):
    channel = {"name": "a", "url": "https://youtube.com/@a", "quality": "480p"}
    server.GLOBALS.update(proxy="", output_dir="/tmp", poll_interval=1)
    server.CONTROL["a"] = {"pause": threading.Event(), "remove": threading.Event(), "stop_now": threading.Event()}
    checks = []
    def check(*args):
        checks.append(True)
        if len(checks) == 3:
            server.set_state("a", last_error=None, backoff_seconds=0)
            return None
        return {"video_id": "abc", "title": "show"}
    monkeypatch.setattr(server, "check_live", check)
    def failed(*args):
        if len([x for x in checks if x]) == 4:
            server.CONTROL["a"]["remove"].set()
        server.set_state("a", last_recording_duration_seconds=1)
        return False
    monkeypatch.setattr(server, "record_stream", failed)
    monkeypatch.setattr(server.time, "sleep", lambda _: None)
    messages = []
    monkeypatch.setattr(server, "log", lambda name, message: messages.append(message))
    server.monitor_channel(channel)
    waits = [int(line.split("waiting ")[1].split("s")[0]) for line in messages if "attempt failed" in line]
    assert waits == [30, 60, 30]


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
