import json
import io
import os
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
    server.BLOCKED_PAGE_CHECKS.clear()
    server.FALLBACK_CHECK_AT.clear()
    server.FALLBACK_NOT_LIVE_AT.clear()
    server.FALLBACK_LAST_SUMMARY.clear()
    server.FALLBACK_FAILURE_KIND.clear()
    server.WORKING_CLIENTS.clear()
    monkeypatch.setattr(server, "log", lambda name, message: server.LOG_BUFFER.append(message))


def player_html(live=True):
    data = {"videoDetails": {"videoId": "abc", "title": "A live show", "isLiveContent": live}, "playabilityStatus": {"status": "OK"}, "streamingData": {"hlsManifestUrl": "https://example/live"}}
    return "<script>ytInitialPlayerResponse = " + json.dumps(data) + ";</script>"


def bot_check_html():
    data = {"playabilityStatus": {"status": "LOGIN_REQUIRED",
                                  "reason": "Sign in to confirm that you're not a bot"}}
    return "<script>ytInitialPlayerResponse=" + json.dumps(data) + ";</script>"


def response(text, status_code=200, url="https://www.youtube.com/@a/live"):
    return SimpleNamespace(status_code=status_code, text=text, url=url)


def test_errors_keep_status_and_clear_on_next_success(monkeypatch):
    server.set_state("a", status="idle")
    monkeypatch.setattr(requests, "get", lambda *args, **kwargs: (_ for _ in ()).throw(requests.ConnectionError("down")))
    assert server.check_live("https://youtube.com/@a", "", "a") is None
    assert server.STATE["a"]["status"] == "idle"
    assert "request failed" in server.STATE["a"]["last_error"]
    monkeypatch.setattr(requests, "get", lambda *args, **kwargs: response(player_html()))
    assert server.check_live("https://youtube.com/@a", "", "a")["video_id"] == "abc"
    assert server.STATE["a"]["last_error"] is None


def test_unknown_and_rate_limited_pages_are_errors_with_five_minute_backoff(monkeypatch):
    server.set_state("a", status="idle")
    monkeypatch.setattr(requests, "get", lambda *args, **kwargs: response("consent page"))
    assert server.check_live("https://youtube.com/@a", "", "a") is None
    assert server.STATE["a"]["last_error"].startswith("unexpected page")
    assert server.STATE["a"]["backoff_seconds"] == 300
    monkeypatch.setattr(requests, "get", lambda *args, **kwargs: response("", status_code=429))
    server.check_live("https://youtube.com/@a", "", "a")
    assert server.STATE["a"]["backoff_seconds"] == 300


def test_blocked_page_logs_are_rate_limited_and_recovery_is_logged(monkeypatch):
    now = [1000]
    monkeypatch.setattr(server.time, "time", lambda: now[0])
    monkeypatch.setattr(requests, "get", lambda *args, **kwargs: response("consent page"))
    for _ in range(4):
        server.check_live("https://youtube.com/@a", "", "a")
    assert sum("unexpected page: no player data" in line for line in server.LOG_BUFFER) == 1
    now[0] += 3601
    server.check_live("https://youtube.com/@a", "", "a")
    assert sum("unexpected page: no player data" in line for line in server.LOG_BUFFER) == 2
    monkeypatch.setattr(requests, "get", lambda *args, **kwargs: response(player_html()))
    assert server.check_live("https://youtube.com/@a", "", "a")["video_id"] == "abc"
    assert any("page check recovered after 5 blocked checks" in line for line in server.LOG_BUFFER)


def test_offline_channel_page_is_a_successful_non_live_check(monkeypatch):
    html = ('<script>var ytInitialData = {}; ytInitialPlayerResponse,a.ytInitialReelWatchSequenceResponse; '
            'var o={"a":1}; function later(){return {"b":2};}</script>')
    monkeypatch.setattr(requests, "get", lambda *args, **kwargs: response(html))
    assert server.check_live("https://youtube.com/@a/live", "", "a") is None
    assert server.STATE["a"]["last_error"] is None
    assert server.STATE["a"]["backoff_seconds"] == 0
    assert not any("unexpected page" in line for line in server.LOG_BUFFER)


def test_bot_check_is_blocked_and_rate_limited(monkeypatch):
    now = [1000]
    monkeypatch.setattr(server.time, "time", lambda: now[0])
    monkeypatch.setattr(requests, "get", lambda *args, **kwargs: response(bot_check_html()))
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout="", stderr="Sign in to confirm you're not a bot"))
    for _ in range(2):
        assert server.check_live("https://youtube.com/@a/live", "", "a") is None
    assert server.STATE["a"]["last_error"] == "blocked by YouTube (bot check)"
    assert server.STATE["a"]["backoff_seconds"] == 300
    blocked_lines = [line for line in server.LOG_BUFFER if "blocked by YouTube" in line]
    assert len(blocked_lines) == 1
    assert server.log_level(blocked_lines[0]) == "WARNING"


@pytest.mark.parametrize(("outcomes", "expected"), [
    ([], "transient"),
    (["error"], "transient"),
    (["not_live"], "not_live"),
    (["error", "not_live"], "not_live"),
    (["bot_check"], "bot_check"),
    (["not_live", "bot_check"], "bot_check"),
])
def test_fallback_failure_kind(outcomes, expected):
    assert server._fallback_failure_kind(outcomes) == expected


@pytest.mark.parametrize("text", [
    "Unable to connect to proxy: HTTP Error 503",
    "unable to CONNECT TO PROXY: refused",
    "Tunnel connection failed: 503",
    "tUnNeL CoNnEcTiOn FaIlEd",
])
def test_proxy_failure_output_case_insensitive(text):
    assert server._proxy_failure_in_output(text)


def test_proxy_failure_output_rejects_unrelated_text():
    assert not server._proxy_failure_in_output("network connection failed")


def test_transient_fallback_failure_uses_one_minute_backoff(monkeypatch):
    monkeypatch.setattr(requests, "get", lambda *args, **kwargs: response(bot_check_html()))
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: SimpleNamespace(
        returncode=1, stdout="", stderr="Unable to connect to proxy: 503"))
    assert server.check_live("https://youtube.com/@a/live", "http://proxy", "a") is None
    assert server.STATE["a"]["last_error"] == "live check failed (proxy or network)"
    assert server.STATE["a"]["backoff_seconds"] == 60
    assert server.STATE["a"].get("blocked_since", 0.0) == 0.0


def test_garbage_page_remains_unexpected(monkeypatch):
    monkeypatch.setattr(requests, "get", lambda *args, **kwargs: response("garbage"))
    assert server.check_live("https://youtube.com/@a/live", "", "a") is None
    assert server.STATE["a"]["last_error"].startswith("unexpected page")
    assert server.STATE["a"]["backoff_seconds"] == 300
    assert any("unexpected page: no player data" in line for line in server.LOG_BUFFER)


def test_live_page_still_returns_video_id_and_title(monkeypatch):
    monkeypatch.setattr(requests, "get", lambda *args, **kwargs: response(player_html()))
    assert server.check_live("https://youtube.com/@a/live", "", "a") == {
        "video_id": "abc", "title": "A live show"}


@pytest.mark.parametrize("assignment", ["var ytInitialPlayerResponse = ", "ytInitialPlayerResponse="])
def test_strict_player_assignment_accepts_whitespace_variants(monkeypatch, assignment):
    data = {"videoDetails": {"videoId": "abc", "title": "A live show", "isLiveContent": True},
            "playabilityStatus": {"status": "OK"},
            "streamingData": {"hlsManifestUrl": "https://example/live"}}
    html = "<script>" + assignment + json.dumps(data) + ";</script>"
    monkeypatch.setattr(requests, "get", lambda *args, **kwargs: response(html))
    assert server.check_live("https://youtube.com/@a/live", "", "a") == {
        "video_id": "abc", "title": "A live show"}


def test_strict_player_assignment_still_blocks_bot_check(monkeypatch):
    monkeypatch.setattr(requests, "get", lambda *args, **kwargs: response(bot_check_html()))
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout="", stderr="Sign in to confirm you're not a bot"))
    assert server.check_live("https://youtube.com/@a/live", "", "a") is None
    assert server.STATE["a"]["last_error"] == "blocked by YouTube (bot check)"
    assert server.STATE["a"]["backoff_seconds"] == 300


def test_blocked_page_falls_back_to_ytdlp_and_remembers_client(monkeypatch):
    monkeypatch.setattr(requests, "get", lambda *args, **kwargs: response(bot_check_html()))
    calls = []
    def run(cmd, **kwargs):
        calls.append(cmd)
        if "--extractor-args" in cmd and cmd[cmd.index("--extractor-args") + 1] == "youtube:player_client=tv":
            return SimpleNamespace(returncode=0, stdout="True\tabc123\tA live show\n", stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr="blocked")
    monkeypatch.setattr(subprocess, "run", run)
    assert server.check_live("https://youtube.com/@a/live", "", "a") == {"video_id": "abc123", "title": "A live show"}
    assert server.WORKING_CLIENTS["a"] == "tv"
    assert server.STATE["a"]["blocked_since"] == 0.0
    assert calls[0][0] == server.YTDLP_BIN and "--simulate" in calls[0]


def test_blocked_fallback_retains_backoff_and_blocked_since(monkeypatch):
    now = [1234.5]
    monkeypatch.setattr(server.time, "time", lambda: now[0])
    monkeypatch.setattr(requests, "get", lambda *args, **kwargs: response(bot_check_html()))
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout="", stderr="LOGIN_REQUIRED: not a bot"))
    assert server.check_live("https://youtube.com/@a/live", "", "a") is None
    assert server.STATE["a"]["blocked_since"] == 1234.5
    assert server.STATE["a"]["backoff_seconds"] == 300
    assert server.STATE["a"]["last_error"] == "blocked by YouTube (bot check)"


def test_fallback_rate_limited_per_channel(monkeypatch):
    now = [5000.0]
    monkeypatch.setattr(server.time, "time", lambda: now[0])
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: calls.append(args[0]) or SimpleNamespace(returncode=1, stdout="", stderr="blocked"))
    server.fallback_check_live("https://youtube.com/@a/live", "", "a")
    assert len(calls) == 4
    server.fallback_check_live("https://youtube.com/@a/live", "", "a")
    assert len(calls) == 4
    now[0] += 300
    server.fallback_check_live("https://youtube.com/@a/live", "", "a")
    assert len(calls) == 8


@pytest.mark.parametrize("cookies_exist,expected", [(False, "no"), (True, "yes")])
def test_fallback_logs_all_bot_check_outcomes_and_cookie_use(tmp_path, monkeypatch, cookies_exist, expected):
    cookie_path = tmp_path / "private-cookies.txt"
    server.GLOBALS["cookies_file"] = str(cookie_path)
    if cookies_exist:
        cookie_path.write_text("secret")
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: SimpleNamespace(
        returncode=1, stdout="", stderr="Sign in to confirm you're not a bot"))
    server.fallback_check_live("https://youtube.com/@a/live", "", "a")
    summaries = [line for line in server.LOG_BUFFER if line.startswith("fallback check:")]
    assert len(summaries) == 1
    assert all(f"{client}=bot_check" in summaries[0] for client in server.PLAYER_CLIENTS)
    assert summaries[0].endswith(f"cookies={expected}")


def test_fallback_live_logs_client_and_video_id(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kwargs: SimpleNamespace(
        returncode=0,
        stdout="True\tabc123\tLive\n" if "youtube:player_client=tv" in cmd else "",
        stderr=""))
    assert server.fallback_check_live("https://youtube.com/@a/live", "", "a")["video_id"] == "abc123"
    assert any("fallback check: tv=live (video abc123) cookies=no" in line for line in server.LOG_BUFFER)


def test_fallback_not_currently_live_is_classified(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: SimpleNamespace(
        returncode=1, stdout="", stderr="Video is not currently live"))
    server.fallback_check_live("https://youtube.com/@a/live", "", "a")
    summary = next(line for line in server.LOG_BUFFER if line.startswith("fallback check:"))
    assert "default=not_live (Video is not currently live)" in summary


def test_fallback_not_live_stops_after_first_client_and_clears_state(monkeypatch):
    server.set_state("a", last_error="old error", blocked_since=10.0, backoff_seconds=300)
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: calls.append(args[0]) or SimpleNamespace(
        returncode=1, stdout="", stderr="ERROR: [youtube:tab] @a: The channel is not currently live"))
    assert server.fallback_check_live("https://youtube.com/@a/live", "", "a") is None
    assert len(calls) == 1
    assert server.STATE["a"]["last_error"] is None
    assert server.STATE["a"]["blocked_since"] == 0.0
    assert server.STATE["a"]["backoff_seconds"] == 0
    assert "a" in server.FALLBACK_NOT_LIVE_AT


def test_blocked_check_not_live_stays_clear_during_sixty_second_cooldown(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(server.time, "time", lambda: now[0])
    monkeypatch.setattr(requests, "get", lambda *args, **kwargs: response(bot_check_html()))
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: calls.append(args[0]) or SimpleNamespace(
        returncode=1, stdout="", stderr="The channel is not currently live"))
    assert server.check_live("https://youtube.com/@a/live", "", "a") is None
    assert server.STATE["a"]["last_error"] is None
    assert server.STATE["a"]["blocked_since"] == 0.0
    assert server.STATE["a"]["backoff_seconds"] == 0
    assert not any("blocked by YouTube" in line for line in server.LOG_BUFFER)
    assert len(calls) == 1
    now[0] += 30
    assert server.check_live("https://youtube.com/@a/live", "", "a") is None
    assert server.STATE["a"]["last_error"] is None
    assert server.STATE["a"]["blocked_since"] == 0.0
    assert server.STATE["a"]["backoff_seconds"] == 0
    assert len(calls) == 1
    now[0] += 31
    server.check_live("https://youtube.com/@a/live", "", "a")
    assert len(calls) == 2


def test_fallback_all_bot_checks_keeps_five_minute_cooldown(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(server.time, "time", lambda: now[0])
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: calls.append(args[0]) or SimpleNamespace(
        returncode=1, stdout="", stderr="Sign in to confirm you're not a bot"))
    server.fallback_check_live("https://youtube.com/@a/live", "", "a")
    assert len(calls) == 4
    assert server.STATE["a"]["last_error"] == "blocked by YouTube (bot check)"
    assert server.STATE["a"]["backoff_seconds"] == 300
    now[0] += 61
    server.fallback_check_live("https://youtube.com/@a/live", "", "a")
    assert len(calls) == 4
    now[0] += 239
    server.fallback_check_live("https://youtube.com/@a/live", "", "a")
    assert len(calls) == 8


def test_fallback_later_live_client_clears_not_live_conclusion(monkeypatch):
    server.FALLBACK_NOT_LIVE_AT["a"] = 10.0
    server.FALLBACK_CHECK_AT["a"] = 9.0
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kwargs: SimpleNamespace(
        returncode=0,
        stdout="True\tabc123\tLive\n" if "youtube:player_client=tv" in cmd else "",
        stderr=""))
    assert server.fallback_check_live("https://youtube.com/@a/live", "", "a")["video_id"] == "abc123"
    assert "a" not in server.FALLBACK_NOT_LIVE_AT


def test_fallback_live_kind_uses_sixty_second_cooldown(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(server.time, "time", lambda: now[0])
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: calls.append(args[0]) or SimpleNamespace(
        returncode=1, stdout="", stderr="temporary error"))
    server.FALLBACK_FAILURE_KIND["a"] = "live"
    server.FALLBACK_CHECK_AT["a"] = now[0] - 5
    assert server.fallback_check_live("https://youtube.com/@a/live", "", "a") is None
    assert calls == []
    now[0] += 56  # 61 s since the last check
    server.fallback_check_live("https://youtube.com/@a/live", "", "a")
    assert len(calls) == 4


def test_fallback_summary_throttles_unchanged_and_logs_changed(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(server.time, "time", lambda: now[0])
    outcome = ["blocked"]
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: SimpleNamespace(
        returncode=1, stdout="", stderr=outcome[0]))
    server.fallback_check_live("https://youtube.com/@a/live", "", "a")
    server.FALLBACK_CHECK_AT.clear()
    server.fallback_check_live("https://youtube.com/@a/live", "", "a")
    assert sum(line.startswith("fallback check:") for line in server.LOG_BUFFER) == 1
    outcome[0] = "different failure"
    server.FALLBACK_CHECK_AT.clear()
    server.fallback_check_live("https://youtube.com/@a/live", "", "a")
    assert sum(line.startswith("fallback check:") for line in server.LOG_BUFFER) == 2


def test_fallback_summary_never_logs_cookie_path(monkeypatch, tmp_path):
    cookie_path = str(tmp_path / "secret-cookies.txt")
    server.GLOBALS["cookies_file"] = cookie_path
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: SimpleNamespace(
        returncode=1, stdout="", stderr=f"failed to read {cookie_path}"))
    server.fallback_check_live("https://youtube.com/@a/live", "", "a")
    assert all(cookie_path not in line for line in server.LOG_BUFFER)
    summary = next(line for line in server.LOG_BUFFER if line.startswith("fallback check:"))
    assert "<cookies>" in summary


@pytest.mark.parametrize("cookies_file,exists,expected", [
    ("", False, "cookies: not configured"),
    ("configured-cookie-file", True, "cookies: configured, file present"),
    ("configured-cookie-file", False, "cookies: configured, file missing"),
])
def test_startup_logs_cookie_configuration_without_path(monkeypatch, tmp_path, cookies_file, exists, expected):
    configured_path = str(tmp_path / cookies_file) if cookies_file else ""
    if exists:
        open(configured_path, "w").close()
    monkeypatch.setattr(server, "load_config", lambda: {"cookies_file": configured_path, "channels": []})
    monkeypatch.setattr(server, "server_version", lambda: "test")
    monkeypatch.setattr(server.api_server, "start_api", lambda *args: None)
    monkeypatch.setattr(server.threading, "Thread", lambda **kwargs: SimpleNamespace(start=lambda: None))
    class StopMain(Exception):
        pass
    monkeypatch.setattr(server.time, "sleep", lambda seconds: (_ for _ in ()).throw(StopMain()))
    with pytest.raises(StopMain):
        server.main()
    cookie_lines = [line for line in server.LOG_BUFFER if line.startswith("cookies:")]
    assert cookie_lines == [expected]
    if configured_path:
        assert configured_path not in "\n".join(cookie_lines)


def test_cookies_flag_only_when_configured_file_exists(tmp_path, monkeypatch):
    cookie_path = tmp_path / "private-cookies.txt"
    server.GLOBALS["cookies_file"] = str(cookie_path)
    assert server._cookies_args() == []
    cookie_path.write_text("secret-cookie-value")
    assert server._cookies_args() == ["--cookies", str(cookie_path)]
    assert "secret-cookie-value" not in "\n".join(server.LOG_BUFFER)


def test_recording_command_uses_remembered_client_and_existing_cookies(tmp_path, monkeypatch):
    server.GLOBALS.update(min_free_gb=0, cookies_file=str(tmp_path / "cookies.txt"))
    (tmp_path / "cookies.txt").write_text("private-value")
    server.WORKING_CLIENTS["a"] = "mweb"
    monkeypatch.setattr(server.shutil, "disk_usage", lambda p: SimpleNamespace(free=10**12))
    monkeypatch.setattr(server, "finalize_recording_output", lambda *args: None)
    seen = []
    monkeypatch.setattr(server.subprocess, "Popen", lambda cmd, **kwargs: seen.append(cmd) or FakeProcess(0, ""))
    assert server.record_stream("a", "abc123", "show", "", str(tmp_path), "480p") is False
    assert "--cookies" in seen[0] and str(tmp_path / "cookies.txt") in seen[0]
    assert "--extractor-args" in seen[0]
    assert "youtube:player_client=mweb" in seen[0]
    assert "private-value" not in "\n".join(server.LOG_BUFFER)


def test_recording_bot_check_retries_once_with_next_client(tmp_path, monkeypatch):
    server.GLOBALS["min_free_gb"] = 0
    monkeypatch.setattr(server.shutil, "disk_usage", lambda p: SimpleNamespace(free=10**12))
    monkeypatch.setattr(server, "finalize_recording_output", lambda *args: None)
    commands = []
    processes = iter((FakeProcess(1, "ERROR: Sign in to confirm that you're not a bot\n"), FakeProcess(0, "")))
    monkeypatch.setattr(server.subprocess, "Popen", lambda cmd, **kwargs: commands.append(cmd) or next(processes))
    assert server.record_stream("a", "abc123", "show", "", str(tmp_path), "480p") is False
    assert len(commands) == 2
    assert "youtube:player_client=tv" in commands[1]


def test_blocked_check_followed_by_offline_page_recovers_once(monkeypatch):
    monkeypatch.setattr(requests, "get", lambda *args, **kwargs: response(bot_check_html()))
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: SimpleNamespace(
        returncode=1, stdout="", stderr="Sign in to confirm that you're not a bot"))
    server.check_live("https://youtube.com/@a/live", "", "a")
    offline = '<script>ytInitialData = {}; ytInitialPlayerResponse,a.ytInitialReelWatchSequenceResponse;</script>'
    monkeypatch.setattr(requests, "get", lambda *args, **kwargs: response(offline))
    assert server.check_live("https://youtube.com/@a/live", "", "a") is None
    assert server.check_live("https://youtube.com/@a/live", "", "a") is None
    recovery = [line for line in server.LOG_BUFFER if "page check recovered after" in line]
    assert len(recovery) == 1
    assert server.log_level(recovery[0]) == "INFO"


def _record_stream_with_saved_file(tmp_path, monkeypatch, size, duration, live_check=None, output=None):
    server.GLOBALS["min_free_gb"] = 0
    server.set_state("a", url="https://youtube.com/@a/live", last_error=None)
    monkeypatch.setattr(server.shutil, "disk_usage", lambda p: SimpleNamespace(free=10**12))
    monkeypatch.setattr(server, "sanitize_filename", lambda title: "show")
    monkeypatch.setattr(server, "datetime", SimpleNamespace(now=lambda: SimpleNamespace(strftime=lambda fmt: "01012026" if "%d" in fmt else "00-00-00")))
    output = output if output is not None else "".join(f"line {n}\n" for n in range(1, 21))
    monkeypatch.setattr(server.subprocess, "Popen", lambda *args, **kwargs: FakeProcess(1, output))
    if live_check is not None:
        monkeypatch.setattr(server, "check_live", live_check)
    ticks = iter((10.0, 10.0 + duration))
    monkeypatch.setattr(server.time, "monotonic", lambda: next(ticks))
    def finalize(output_dir, base_name, channel_name):
        path = tmp_path / f"{base_name}.mp4"
        path.write_bytes(b"x" * size)
        return str(path)
    monkeypatch.setattr(server, "finalize_recording_output", finalize)
    return server.record_stream("a", "abc", "show", "", str(tmp_path), "480p")


def test_nonzero_exit_with_good_saved_recording_still_live_logs_15_lines(tmp_path, monkeypatch):
    server.FALLBACK_CHECK_AT["a"] = 123.0
    assert _record_stream_with_saved_file(
        tmp_path, monkeypatch, 1024 * 1024, 120,
        live_check=lambda *args: {"video_id": "abc", "title": "show"}) is True
    assert server.STATE["a"]["last_error"] is None
    assert not any("yt-dlp failed" in line for line in server.LOG_BUFFER)
    summary = "yt-dlp ended early (code 1) while the stream is still live; recording saved, a new recording will start"
    assert summary in server.LOG_BUFFER
    assert "a" not in server.FALLBACK_CHECK_AT
    assert server.LOG_BUFFER.index("line 6") < server.LOG_BUFFER.index(summary)
    assert [line for line in server.LOG_BUFFER if line.startswith("line ")] == [
        f"line {n}" for n in range(6, 21)]


def test_nonzero_exit_with_good_saved_recording_proxy_failure_clears_fallback_cooldown(tmp_path, monkeypatch):
    server.FALLBACK_CHECK_AT["a"] = 123.0
    assert _record_stream_with_saved_file(
        tmp_path, monkeypatch, 1024 * 1024, 120,
        output="ERROR: Unable to connect to proxy\n") is True
    summary = "yt-dlp stopped because the proxy failed; the stream may still be live; will retry"
    assert summary in server.LOG_BUFFER
    assert "a" not in server.FALLBACK_CHECK_AT


def test_nonzero_exit_with_good_saved_recording_offline_logs_normal_summary(tmp_path, monkeypatch):
    assert _record_stream_with_saved_file(
        tmp_path, monkeypatch, 1024 * 1024, 120,
        live_check=lambda *args: None) is True
    assert any("yt-dlp finished with code 1 (normal at stream end); recording saved" in line
               for line in server.LOG_BUFFER)


def test_nonzero_exit_with_good_saved_recording_live_check_error_is_unknown(tmp_path, monkeypatch):
    def failed_check(*args):
        server.set_state("a", last_error="request failed: down")
        return None

    assert _record_stream_with_saved_file(
        tmp_path, monkeypatch, 1024 * 1024, 120, live_check=failed_check) is True
    assert any("(normal at stream end; live status unknown); recording saved" in line
               for line in server.LOG_BUFFER)


@pytest.mark.parametrize(("size", "duration"), [(1024 * 1024 - 1, 120), (1024 * 1024, 119)])
def test_nonzero_exit_with_small_or_short_saved_recording_still_fails(tmp_path, monkeypatch, size, duration):
    assert _record_stream_with_saved_file(tmp_path, monkeypatch, size, duration) is False
    assert any("yt-dlp failed; output tail" in line for line in server.LOG_BUFFER)


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
    assert status["channels"][0]["blocked_since"] == 0.0
    assert {k: status["channels"][0][k] for k in ("title", "last_error", "video_id")} == {"title": "", "last_error": "", "video_id": ""}
    assert status["channels"][0]["last_check_at"] == 0.0
    assert status["channels"][0]["last_live_at"] == 0.0


@pytest.mark.parametrize(("line", "expected"), [
    ("An ERROR occurred", "ERROR"),
    ("yt-dlp failed", "ERROR"),
    ("Could not merge the files", "ERROR"),
    ("Process exited with code 1", "ERROR"),
    ("No output file found", "ERROR"),
    ("Traceback from worker", "ERROR"),
    ("Waiting 30 seconds", "WARNING"),
    ("Backoff active", "WARNING"),
    ("Startup recovery skipped recent parts", "WARNING"),
    ("only a video part was found", "WARNING"),
    ("only an audio part was found", "WARNING"),
    ("low disk space", "WARNING"),
    ("recording ended under 120s", "WARNING"),
    ("Recording saved to show.mp4", "SUCCESS"),
    ("finalized old recording", "SUCCESS"),
    ("merged video and audio", "SUCCESS"),
    ("channel check started", "INFO"),
    ("ERROR and waiting for retry", "ERROR"),
    ("waiting; recording saved", "WARNING"),
])
def test_log_level_classification_and_priority(line, expected):
    assert server.log_level(line) == expected


def test_log_buffer_retains_2000_plain_string_lines():
    server.LOG_BUFFER.extend(f"entry {index}" for index in range(2005))
    assert len(server.LOG_BUFFER) == 2000
    assert server.LOG_BUFFER[0] == "entry 5"
    assert server.LOG_BUFFER[-1] == "entry 2004"


def test_clear_logs_leaves_only_the_clear_message():
    server.LOG_BUFFER.extend(("old one", "old two", "old three"))
    assert server.clear_logs() == 3
    assert list(server.LOG_BUFFER) == ["Server log cleared from the app."]


def test_server_version_has_safe_fallback(monkeypatch):
    monkeypatch.setattr(server.subprocess, "check_output", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("no git")))
    assert server.server_version() == "unknown"


def test_monitor_rechecks_and_starts_a_new_recording_after_one_ends(monkeypatch):
    channel = {"name": "a", "url": "https://youtube.com/@a", "quality": "480p"}
    server.GLOBALS.update(proxy="", output_dir="/tmp", poll_interval=1)
    server.CONTROL["a"] = {"pause": threading.Event(), "remove": threading.Event()}
    monkeypatch.setattr(server, "check_live", lambda *args: {"video_id": "abc", "title": "show"})
    starts = []
    checked_times = []
    def record(*args):
        starts.append(args)
        checked_times.append((server.STATE["a"]["last_check_at"], server.STATE["a"]["last_live_at"]))
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
    assert all(isinstance(value, float) and value > 0 for pair in checked_times for value in pair)


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
    assert server.STATE["a"]["last_error"]
    assert any("Could not convert" in line for line in server.LOG_BUFFER)


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
    config.write_text(json.dumps({"channels": [], "api_token": "", "min_free_gb": 4,
                                  "output_dir": str(tmp_path / "recordings")}))
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


def test_output_reader_saves_timestamped_log_masks_proxy_and_filters_debug(tmp_path):
    from collections import deque
    lines = deque(maxlen=200)
    path = tmp_path / "recording.log"
    stream = io.StringIO("[debug] proxy http://user:secret@example.test:8080\nERROR: failed\n")
    server.read_process_output(stream, lines, str(path), "http://user:secret@example.test:8080")
    saved = path.read_text()
    assert "[20" in saved and "] [debug] proxy http://[masked]@example.test:8080" in saved
    assert "] ERROR: failed" in saved
    assert list(lines) == ["ERROR: failed"]
    assert "secret" not in saved


def test_prune_ytdlp_logs_keeps_newest_20(tmp_path):
    paths = []
    for index in range(25):
        path = tmp_path / f"{index:02}.log"
        path.write_text(str(index))
        os.utime(path, (index, index))
        paths.append(path)
    server.prune_ytdlp_logs(str(tmp_path), keep=20)
    assert len(list(tmp_path.glob("*.log"))) == 20
    assert all(not paths[index].exists() for index in range(5))
    assert all(paths[index].exists() for index in range(5, 25))


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


def _probe_json(path):
    if ".f135." in path:
        streams = [{"codec_type": "video", "codec_name": "h264"}]
    elif ".f140." in path:
        streams = [{"codec_type": "audio", "codec_name": "aac"}]
    elif ".f141." in path:
        streams = [{"codec_type": "video", "codec_name": "vp9"}]
    elif ".f142." in path:
        streams = [{"codec_type": "audio", "codec_name": "opus"}]
    else:
        streams = [{"codec_type": "video", "codec_name": "h264"}]
    return SimpleNamespace(returncode=0, stdout=json.dumps({"streams": streams}))


def test_parts_are_merged_then_deleted(tmp_path, monkeypatch):
    base = "show_20102026_02-26-35"
    video = tmp_path / f"{base}.f135.mp4"
    audio = tmp_path / f"{base}.f140.m4a"
    video.write_bytes(b"video")
    audio.write_bytes(b"audio")
    commands = []
    def run(cmd, **kwargs):
        if cmd[0] == server.FFPROBE_BIN:
            return _probe_json(cmd[-1])
        commands.append(cmd)
        (tmp_path / f"{base}.mp4").write_bytes(b"merged")
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(server.subprocess, "run", run)
    result = server.finalize_recording_output(str(tmp_path), base, "a")
    assert result == str(tmp_path / f"{base}.mp4")
    assert not video.exists() and not audio.exists()
    assert (tmp_path / f"{base}.mp4").read_bytes() == b"merged"
    assert "-map" in commands[0] and "-c" in commands[0] and "copy" in commands[0]
    assert "-map" in commands[0] and "0:v:0" in commands[0] and "1:a:0" in commands[0]


def test_existing_final_file_is_used_when_there_are_no_parts(tmp_path, monkeypatch):
    final = tmp_path / "show.mkv"
    final.write_bytes(b"complete recording")
    monkeypatch.setattr(server.subprocess, "run", lambda *args, **kwargs: pytest.fail("ffmpeg should not run"))
    assert server.finalize_recording_output(str(tmp_path), "show", "a") == str(final)


def test_incompatible_parts_merge_to_mkv(tmp_path, monkeypatch):
    base = "show"
    (tmp_path / f"{base}.f135.mp4").write_bytes(b"video")
    (tmp_path / f"{base}.f140.m4a").write_bytes(b"audio")
    merge = []
    def run(cmd, **kwargs):
        if cmd[0] == server.FFPROBE_BIN:
            codec = "hevc" if ".f135." in cmd[-1] else "opus"
            stream_type = "video" if codec == "hevc" else "audio"
            return SimpleNamespace(returncode=0, stdout=json.dumps({"streams": [{"codec_type": stream_type,
                                                                                    "codec_name": codec}]}))
        merge.append(cmd)
        (tmp_path / f"{base}.mkv").write_bytes(b"merged")
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(server.subprocess, "run", run)
    assert server.finalize_recording_output(str(tmp_path), base, "a").endswith("show.mkv")


def test_failed_merge_keeps_every_part_and_reports_failure(tmp_path, monkeypatch):
    base = "show"
    video = tmp_path / f"{base}.f135.mp4"
    audio = tmp_path / f"{base}.f140.m4a"
    video.write_bytes(b"video")
    audio.write_bytes(b"audio")
    monkeypatch.setattr(server.subprocess, "run", lambda cmd, **kwargs: (
        _probe_json(cmd[-1]) if cmd[0] == server.FFPROBE_BIN else SimpleNamespace(returncode=1, stdout="", stderr="bad")
    ))
    assert server.finalize_recording_output(str(tmp_path), base, "a") is False
    assert video.exists() and audio.exists()
    assert any("Could not merge parts" in line and video.name in line and audio.name in line
               for line in server.LOG_BUFFER)


def test_merge_retries_once_with_ignore_err(tmp_path, monkeypatch):
    base = "show"
    (tmp_path / f"{base}.f135.mp4").write_bytes(b"video")
    (tmp_path / f"{base}.f140.m4a").write_bytes(b"audio")
    merge_commands = []
    def run(cmd, **kwargs):
        if cmd[0] == server.FFPROBE_BIN:
            return _probe_json(cmd[-1])
        merge_commands.append(cmd)
        if len(merge_commands) == 2:
            (tmp_path / f"{base}.mp4").write_bytes(b"merged")
            return SimpleNamespace(returncode=0)
        return SimpleNamespace(returncode=1)
    monkeypatch.setattr(server.subprocess, "run", run)
    assert server.finalize_recording_output(str(tmp_path), base, "a").endswith(f"{base}.mp4")
    assert len(merge_commands) == 2
    assert "-err_detect" in merge_commands[1] and "ignore_err" in merge_commands[1]


def test_single_video_part_is_kept_and_not_reported_saved(tmp_path, monkeypatch):
    base = "show"
    video = tmp_path / f"{base}.f135.mp4"
    video.write_bytes(b"video")
    monkeypatch.setattr(server.subprocess, "run", lambda *args, **kwargs: _probe_json(str(video)))
    assert server.finalize_recording_output(str(tmp_path), base, "a") is False
    assert video.exists()
    assert any("only a video part was found" in line for line in server.LOG_BUFFER)
    assert not any("Recording saved" in line for line in server.LOG_BUFFER)


def test_part_merge_order_is_independent_of_listdir_order(tmp_path, monkeypatch):
    base = "show"
    names = [f"{base}.f142.m4a", f"{base}.f141.mp4", f"{base}.f140.m4a", f"{base}.f135.mp4"]
    for name in names:
        (tmp_path / name).write_bytes(name.encode())
    real_listdir = server.os.listdir
    monkeypatch.setattr(server.os, "listdir", lambda path: list(reversed(real_listdir(path))))
    merge = []
    def run(cmd, **kwargs):
        if cmd[0] == server.FFPROBE_BIN:
            return _probe_json(cmd[-1])
        merge.append(cmd)
        (tmp_path / f"{base}.mp4").write_bytes(b"merged")
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(server.subprocess, "run", run)
    assert server.finalize_recording_output(str(tmp_path), base, "a").endswith(f"{base}.mp4")
    assert merge[0][merge[0].index("-i") + 1].endswith(".f135.mp4")
    assert merge[0][merge[0].index("-i", merge[0].index("-i") + 1) + 1].endswith(".f140.m4a")


def test_nonzero_ytdlp_exit_salvages_parts_but_returns_failure(tmp_path, monkeypatch):
    server.GLOBALS["min_free_gb"] = 0
    monkeypatch.setattr(server.shutil, "disk_usage", lambda p: SimpleNamespace(free=10**12))
    monkeypatch.setattr(server, "sanitize_filename", lambda title: "show")
    monkeypatch.setattr(server, "datetime", SimpleNamespace(now=lambda: SimpleNamespace(strftime=lambda fmt: "20102026" if "%d" in fmt else "02-26-35")))
    base = "show_20102026_02-26-35"
    def popen(*args, **kwargs):
        (tmp_path / f"{base}.f135.mp4").write_bytes(b"video")
        (tmp_path / f"{base}.f140.m4a").write_bytes(b"audio")
        return FakeProcess(1, "failed\n")
    monkeypatch.setattr(server.subprocess, "Popen", popen)
    def run(cmd, **kwargs):
        if cmd[0] == server.FFPROBE_BIN:
            return _probe_json(cmd[-1])
        (tmp_path / f"{base}.mp4").write_bytes(b"merged")
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(server.subprocess, "run", run)
    assert server.record_stream("a", "abc", "show", "", str(tmp_path), "480p") is False
    assert (tmp_path / f"{base}.mp4").read_bytes() == b"merged"
    assert not (tmp_path / f"{base}.f135.mp4").exists()
    assert "yt-dlp exited with code 1" in server.STATE["a"]["last_error"]


def test_user_stop_salvages_parts_without_last_error(tmp_path, monkeypatch):
    server.GLOBALS["min_free_gb"] = 0
    stop = threading.Event()
    pause = threading.Event()
    server.CONTROL["a"] = {"pause": pause, "remove": threading.Event(), "stop_now": stop}
    monkeypatch.setattr(server.shutil, "disk_usage", lambda p: SimpleNamespace(free=10**12))
    monkeypatch.setattr(server, "sanitize_filename", lambda title: "show")
    monkeypatch.setattr(server, "datetime", SimpleNamespace(now=lambda: SimpleNamespace(strftime=lambda fmt: "20102026" if "%d" in fmt else "02-26-35")))
    base = "show_20102026_02-26-35"
    process = FakeProcess(None)
    def popen(*args, **kwargs):
        (tmp_path / f"{base}.f135.mp4").write_bytes(b"video")
        (tmp_path / f"{base}.f140.m4a").write_bytes(b"audio")
        return process
    monkeypatch.setattr(server.subprocess, "Popen", popen)
    monkeypatch.setattr(server.time, "sleep", lambda _: (stop.set(), pause.set()))
    monkeypatch.setattr(server.os, "killpg", lambda pid, sig: setattr(process, "returncode", 130))
    def run(cmd, **kwargs):
        if cmd[0] == server.FFPROBE_BIN:
            return _probe_json(cmd[-1])
        (tmp_path / f"{base}.mp4").write_bytes(b"merged")
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(server.subprocess, "run", run)
    assert server.record_stream("a", "abc", "show", "", str(tmp_path), "480p") is True
    assert (tmp_path / f"{base}.mp4").exists()
    assert server.STATE["a"].get("last_error") is None
    assert server.STATE["a"]["status"] == "paused"


def test_startup_recovery_merges_old_parts_and_cleans_only_old_empty_parts(tmp_path, monkeypatch):
    now = 2_000_000_000
    old_video = tmp_path / "old.f135.mp4"
    old_audio = tmp_path / "old.f140.m4a"
    new_video = tmp_path / "new.f135.mp4"
    active_video = tmp_path / "active.f135.mp4"
    empty_old = tmp_path / "empty.f137.mp4"
    empty_new = tmp_path / "empty.f138.mp4"
    for path, data in ((old_video, b"v"), (old_audio, b"a"), (new_video, b"v"),
                       (active_video, b"v"), (empty_old, b""), (empty_new, b"")):
        path.write_bytes(data)
    for path in (old_video, old_audio, active_video, empty_old):
        os.utime(path, (now - 700, now - 700))
    os.utime(new_video, (now - 100, now - 100))
    os.utime(empty_new, (now - 100, now - 100))
    monkeypatch.setattr(server.time, "time", lambda: now)
    monkeypatch.setattr(server, "active_prefixes", lambda: ["active"])
    merges = []
    def run(cmd, **kwargs):
        if cmd[0] == server.FFPROBE_BIN:
            return _probe_json(cmd[-1])
        merges.append(cmd)
        (tmp_path / "old.mp4").write_bytes(b"merged")
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(server.subprocess, "run", run)
    server.recover_recordings(str(tmp_path))
    assert (tmp_path / "old.mp4").exists()
    assert not old_video.exists() and not old_audio.exists()
    assert new_video.exists() and active_video.exists()
    assert not empty_old.exists()
    assert empty_new.exists()
    assert len(merges) == 1


def test_main_logs_version_before_starting_workers(monkeypatch, tmp_path):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"channels": [], "api_token": "", "min_free_gb": 4,
                                  "output_dir": str(tmp_path / "recordings")}))
    monkeypatch.setattr(server, "CONFIG_PATH", str(config))
    monkeypatch.setattr(server, "server_version", lambda: "deadbee")
    monkeypatch.setattr(server.api_server, "start_api", lambda *args: None)
    monkeypatch.setattr(server.time, "sleep", lambda _: (_ for _ in ()).throw(RuntimeError("stop")))
    with pytest.raises(RuntimeError, match="stop"):
        server.main()
    assert any("version deadbee" in line for line in server.LOG_BUFFER)
    assert server.GLOBALS["min_free_gb"] == 4
