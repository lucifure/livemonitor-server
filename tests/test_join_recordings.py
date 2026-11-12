"""Integration tests for tools/join_recordings.py on synthetic media.

They need ffmpeg, ffprobe and numpy and are skipped (with a reason) without them.
"""
import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")
if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
    pytest.skip("ffmpeg/ffprobe not available", allow_module_level=True)
try:
    subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
except (OSError, subprocess.SubprocessError):
    pytest.skip("ffmpeg does not start here", allow_module_level=True)

TOOL = Path(__file__).resolve().parents[1] / "tools" / "join_recordings.py"


def ffmpeg(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *map(str, args)], check=True)


def duration(path):
    return float(subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)], text=True))


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def audio4k(path, start=0, seconds=None):
    cmd = ["ffmpeg", "-v", "error", "-ss", str(start), "-i", str(path)]
    if seconds:
        cmd += ["-t", str(seconds)]
    cmd += ["-vn", "-ac", "1", "-ar", "4000", "-f", "s16le", "pipe:1"]
    return np.frombuffer(subprocess.check_output(cmd), dtype="<i2").astype(np.float64)


def make_master(tmp_path, name, seed):
    """34 s VP9+AAC master with a keyframe every second and unique, speech-like audio."""
    path = tmp_path / name
    audio = (f"anoisesrc=color=pink:sample_rate=44100:duration=34:seed={seed},"
             "volume='0.25+0.2*sin(2*PI*t/3.1)+0.1*sin(2*PI*t/0.7)':eval=frame")
    ffmpeg("-f", "lavfi", "-i", "testsrc2=size=160x90:rate=12:duration=34", "-f", "lavfi", "-i", audio,
           "-c:v", "libvpx-vp9", "-deadline", "realtime", "-cpu-used", "8", "-crf", "35", "-b:v", "0",
           "-g", "12", "-keyint_min", "12", "-c:a", "aac", "-b:a", "96k", path)
    return path


@pytest.fixture
def master(tmp_path):
    return make_master(tmp_path, "master.mkv", seed=4)


def part(tmp_path, master_path, start, length, name):
    out = tmp_path / name
    ffmpeg("-ss", start, "-i", master_path, "-t", length, "-c", "copy", out)
    return out


def run(*args):
    return subprocess.run([sys.executable, str(TOOL), *map(str, args)], capture_output=True, text=True)


def assert_matches_master(output, master_path, check_from, check_to, max_shift=0.15, min_corr=0.9):
    """Joined audio must follow the master's audio second by second (no repeat, no skip).

    A stream-copy join can only cut on video-frame boundaries, so the seam may be off by
    about one video frame; allow up to max_shift seconds of lag but nothing more.
    """
    joined = audio4k(output, 0, check_to + 2)
    ref = audio4k(master_path, 0, check_to + 2)
    pad = int(max_shift * 4000)
    for second in range(int(check_from), int(check_to)):
        a = joined[second * 4000:(second + 1) * 4000]
        a = (a - a.mean()) / (a.std() + 1e-9)
        best = 0.0
        for lag in range(-pad, pad + 1):
            lo = second * 4000 + lag
            if lo < 0:
                continue
            b = ref[lo:lo + 4000]
            if len(b) < 4000:
                continue
            b = (b - b.mean()) / (b.std() + 1e-9)
            best = max(best, float(np.dot(a, b) / 4000))
        assert best > min_corr, f"audio differs from the original at {second}s (best corr {best:.2f})"


def test_two_files_stream_copy(master, tmp_path):
    first = part(tmp_path, master, 0, 20, "p1.mkv")
    second = part(tmp_path, master, 8, 20, "p2.mkv")
    before = (digest(first), digest(second))
    out = tmp_path / "joined.mkv"
    result = run(first, second, "--out", out)
    assert result.returncode == 0, result.stderr + result.stdout
    assert "stream copy" in result.stdout
    assert abs(duration(out) - 28) < 1.5
    assert_matches_master(out, master, 2, 26)
    assert (digest(first), digest(second)) == before


def test_three_file_chain(master, tmp_path):
    first = part(tmp_path, master, 0, 20, "p1.mkv")
    second = part(tmp_path, master, 8, 20, "p2.mkv")
    third = part(tmp_path, master, 16, 18, "p3.mkv")
    out = tmp_path / "joined3.mkv"
    result = run(first, second, third, "--out", out)
    assert result.returncode == 0, result.stderr + result.stdout
    assert abs(duration(out) - 34) < 1.5
    assert_matches_master(out, master, 2, 32)


def test_different_codec_reencodes(master, tmp_path):
    first = part(tmp_path, master, 0, 20, "p1.mkv")
    second = tmp_path / "p2.mp4"
    ffmpeg("-ss", 8, "-i", master, "-t", 20, "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", second)
    out = tmp_path / "joined.mp4"
    result = run(first, second, "--out", out)
    assert result.returncode == 0, result.stderr + result.stdout
    assert "re-encode" in result.stdout
    assert abs(duration(out) - 28) < 2.5
    assert_matches_master(out, master, 2, 26, min_corr=0.75)  # audio is encoded twice here


def test_dry_run_reports_offset_and_writes_nothing(master, tmp_path):
    first = part(tmp_path, master, 0, 20, "p1.mkv")
    second = part(tmp_path, master, 8, 20, "p2.mkv")
    out = tmp_path / "joined.mkv"
    result = run(first, second, "--dry-run", "--out", out)
    assert result.returncode == 0, result.stderr
    import re
    offset = float(re.search(r"starts ([0-9.]+) s into", result.stdout).group(1))
    assert 6.5 <= offset <= 8.5  # stream-copy cuts snap to a keyframe, so not exactly 8
    expected = float(re.search(r"Expected length: ([0-9.]+) s", result.stdout).group(1))
    assert abs(expected - 28) < 1.5
    assert not out.exists() and not list(tmp_path.glob("join_*")) and not Path(str(out) + ".tmp").exists()


def test_no_overlap_is_not_joined(master, tmp_path):
    first = part(tmp_path, master, 0, 12, "p1.mkv")
    second = part(tmp_path, master, 20, 12, "p2.mkv")
    out = tmp_path / "must-not-exist.mkv"
    before = (digest(first), digest(second))
    result = run(first, second, "--out", out)
    assert result.returncode == 2
    assert "NOT JOINED" in result.stderr
    assert not out.exists() and not Path(str(out) + ".tmp").exists()
    assert (digest(first), digest(second)) == before


def test_unrelated_audio_is_not_joined(master, tmp_path):
    other = make_master(tmp_path, "other.mkv", seed=99)
    first = part(tmp_path, master, 0, 20, "p1.mkv")
    second = part(tmp_path, other, 8, 20, "p2.mkv")
    result = run(first, second, "--dry-run")
    assert result.returncode == 2, result.stdout
    assert "NOT JOINED" in result.stderr


def test_wrong_order_is_rejected(master, tmp_path):
    first = part(tmp_path, master, 0, 20, "p1.mkv")
    second = part(tmp_path, master, 8, 20, "p2.mkv")
    result = run(second, first, "--dry-run")
    assert result.returncode == 2
    assert "NOT JOINED" in result.stderr


def test_existing_output_is_never_overwritten(master, tmp_path):
    first = part(tmp_path, master, 0, 20, "p1.mkv")
    second = part(tmp_path, master, 8, 20, "p2.mkv")
    out = tmp_path / "joined.mkv"
    out.write_bytes(b"keep me")
    result = run(first, second, "--out", out)
    assert result.returncode == 1
    assert out.read_bytes() == b"keep me"
