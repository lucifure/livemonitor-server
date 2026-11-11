"""Synthetic integration tests for the standalone overlapping recording joiner."""
import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

np = pytest.importorskip("numpy", reason="joiner integration tests require numpy")
if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
    pytest.skip("joiner integration tests require ffmpeg and ffprobe", allow_module_level=True)
try:
    subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
except (OSError, subprocess.SubprocessError):
    pytest.skip("ffmpeg is installed but not runnable in this environment", allow_module_level=True)

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "join_recordings.py"


def run_ffmpeg(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *map(str, args)], check=True)


def duration(path):
    return float(subprocess.check_output([
        "ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)
    ], text=True))


@pytest.fixture
def media_factory(tmp_path):
    master = tmp_path / "master.mkv"
    # Time-varying noise gives a unique, repeatable fingerprint that survives
    # VP9/Opus and H.264/AAC encoding better than raw sample matching.
    audio = ("anoisesrc=color=white:sample_rate=48000:duration=34:seed=4,"
             "volume='0.25+0.18*sin(2*PI*t/5)+0.12*sin(2*PI*t/2.7)':eval=frame")
    run_ffmpeg("-f", "lavfi", "-i", "testsrc2=size=160x90:rate=12:duration=34",
               "-f", "lavfi", "-i", audio, "-map", "0:v", "-map", "1:a",
               "-c:v", "ffv1", "-c:a", "pcm_s16le", master)

    def make(start, length, index):
        path = tmp_path / (f"part{index}.mkv" if index == 1 else f"part{index}.mp4")
        if index == 1:
            run_ffmpeg("-ss", start, "-i", master, "-t", length, "-c:v", "libvpx-vp9",
                       "-deadline", "realtime", "-cpu-used", "8", "-crf", "35", "-b:v", "0",
                       "-c:a", "libopus", path)
        else:
            run_ffmpeg("-ss", start, "-i", master, "-t", length, "-c:v", "libx264",
                       "-preset", "ultrafast", "-crf", "30", "-c:a", "aac", path)
        return path
    return make


def run_tool(*args):
    return subprocess.run([sys.executable, str(TOOL), *map(str, args)],
                          capture_output=True, text=True)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def assert_audio_continuous(path, cuts):
    raw = subprocess.check_output(["ffmpeg", "-v", "error", "-i", str(path), "-vn", "-ac", "1",
                                   "-ar", "4000", "-f", "s16le", "pipe:1"])
    samples = np.frombuffer(raw, dtype="<i2").astype(np.float32)
    for seconds in cuts:
        cut = int(seconds * 4000)
        before = np.sqrt(np.mean(samples[cut - 2000:cut] ** 2))
        after = np.sqrt(np.mean(samples[cut:cut + 2000] ** 2))
        assert before > 100 and after > 100
        assert max(before, after) / min(before, after) < 2.0


def test_two_file_different_codec_join_duration_and_audio(media_factory, tmp_path):
    first = media_factory("0", "20", 1)
    second = media_factory("8", "18", 2)
    before = (digest(first), digest(second))
    output = tmp_path / "joined.mp4"
    result = run_tool(first, second, "--out", output)
    assert result.returncode == 0, result.stderr + result.stdout
    assert abs(duration(output) - 26) < 2
    assert (digest(first), digest(second)) == before
    # Decode PCM and reject an audible full-scale discontinuity near the
    # expected midpoint cut (14 seconds into this 26-second output).
    assert_audio_continuous(output, [14])


def test_three_file_chain_and_dry_run_writes_nothing(media_factory, tmp_path):
    first = media_factory("0", "20", 1)
    second = media_factory("8", "20", 2)
    third = media_factory("16", "18", 3)
    output = tmp_path / "joined3.mp4"
    preview = run_tool(first, second, third, "--dry-run", "--out", output)
    assert preview.returncode == 0, preview.stderr + preview.stdout
    assert "Expected final duration" in preview.stdout
    assert "B offset 8." in preview.stdout.splitlines()[0]
    assert not output.exists() and not Path(str(output) + ".tmp").exists()
    result = run_tool(first, second, third, "--out", output)
    assert result.returncode == 0, result.stderr + result.stdout
    assert abs(duration(output) - 34) < 2
    assert_audio_continuous(output, [14, 22])


def test_non_overlapping_files_return_two_without_output(media_factory, tmp_path):
    first = media_factory("0", "12", 1)
    second = media_factory("20", "12", 2)
    output = tmp_path / "must-not-exist.mp4"
    before = (digest(first), digest(second))
    result = run_tool(first, second, "--out", output)
    assert result.returncode == 2
    assert "NOT JOINED:" in result.stderr
    assert not output.exists() and not Path(str(output) + ".tmp").exists()
    assert (digest(first), digest(second)) == before
