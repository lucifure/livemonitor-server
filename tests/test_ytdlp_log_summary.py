import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ytdlp_log_summary import summarize_ytdlp_log  # noqa: E402

SAMPLE = """\
[2026-11-13 17:58:42] [debug] yt-dlp version 2026.10.26 [abc123] (pip)
[2026-11-13 17:58:42] [youtube] JWZ: Downloading webpage
[2026-11-13 17:58:45] [info] JWZ: Downloading 1 format(s): 242+140
[2026-11-13 18:08:38] [download] fragment not found; Skipping fragment 131 ...
[2026-11-13 18:08:51] [download] fragment not found; Skipping fragment 132 ...
[2026-11-13 18:09:11] [youtube] JWZ: Downloading webpage
[2026-11-13 18:09:12] [debug] [youtube] JWZ: tv_downgraded player response playability status: UNPLAYABLE
[2026-11-13 18:09:12] [debug] [youtube] JWZ: web player response playability status: OK
[2026-11-13 18:09:13] [debug] Fetching https://example.com/seg?sig=SECRETVALUE&other=1 token=SECRET2
[2026-11-13 18:09:21] [youtube] JWZ: Downloading webpage
[2026-11-13 18:09:26] 1: [download] 100% of    4.20MiB in 00:10:40 at 6.71KiB/s
[2026-11-13 18:09:26] [Merger] Merging formats into "out.mkv"
"""


def write(tmp_path, text):
    path = tmp_path / "sample.log"
    path.write_text(text)
    return str(path)


def test_counts_and_lists(tmp_path):
    out = "\n".join(summarize_ytdlp_log(write(tmp_path, SAMPLE)))
    assert "12 lines, log times 17:58:42 to 18:09:26" in out
    assert "yt-dlp 2026.10.26, formats 242+140" in out
    assert "skipped fragments 2" in out
    assert "skipped fragments (2): 131@18:08:38, 132@18:08:51" in out
    assert "1x UNPLAYABLE" in out and "1x OK" in out
    assert "latest playability (last 2): 18:09:12 tv_downgraded=UNPLAYABLE" in out
    assert "webpage fetches 3, latest: 17:58:42, 18:09:11, 18:09:21" in out


def test_tail_stops_before_download_finished_and_masks_secrets(tmp_path):
    out = "\n".join(summarize_ytdlp_log(write(tmp_path, SAMPLE)))
    assert "last lines before the download finished:" in out
    assert "SECRETVALUE" not in out and "SECRET2" not in out
    assert "?..." in out
    assert "Merging formats" not in out
    assert "[download] 100%" not in out


def test_missing_file_returns_one_line(tmp_path):
    out = summarize_ytdlp_log(str(tmp_path / "nope.log"))
    assert len(out) == 1 and "cannot read nope.log" in out[0]


def test_output_is_short_and_clipped(tmp_path):
    body = "".join(f"[2026-11-13 18:{i % 60:02d}:00] [download] fragment not found; Skipping fragment {i} ...\n"
                   for i in range(200))
    body += "[2026-11-13 19:00:00] " + "x" * 400 + "\n"
    out = summarize_ytdlp_log(write(tmp_path, body))
    assert len(out) <= 26
    assert all(len(line) <= 150 for line in out)
    assert any("skipped fragments (200, first 12)" in line for line in out)


def test_log_without_timestamps_or_markers(tmp_path):
    out = "\n".join(summarize_ytdlp_log(write(tmp_path, "hello\nworld\n")))
    assert "2 lines, log times ? to ?" in out
    assert "last lines of the log:" in out
    assert "skipped fragments (0): none" in out


def test_server_hook_logs_each_line_and_survives_errors(tmp_path, monkeypatch):
    import livemonitor_server as server

    logged = []
    monkeypatch.setattr(server, "log", lambda channel, message: logged.append((channel, message)))
    server.log_ytdlp_summary("darth_mb", write(tmp_path, SAMPLE))
    assert logged and all(channel == "darth_mb" for channel, _ in logged)
    assert any("skipped fragments (2)" in message for _, message in logged)

    logged.clear()
    monkeypatch.setattr(server, "summarize_ytdlp_log", lambda path: 1 / 0)
    server.log_ytdlp_summary("darth_mb", "x")
    assert len(logged) == 1 and "Could not summarise" in logged[0][1]
