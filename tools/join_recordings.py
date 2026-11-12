#!/usr/bin/env python3
"""Join overlapping recordings of ONE stream into a single file.

Usage: python3 tools/join_recordings.py FILE1 FILE2 [FILE3 ...] [--dry-run] [--out PATH]

Files are given in recording order (oldest first). Each later file must start
INSIDE the previous one (they overlap in time). The tool finds where file N+1
starts inside file N by matching its audio waveform, keeps file N up to that
point, then continues with file N+1. Inputs are never modified.

If every file has the same video and audio codec and picture size, the join is
done by stream copy (lossless, takes seconds, output .mkv). Otherwise every
part is re-encoded to H.264/AAC (slow, output .mp4).

Exit codes: 0 joined (or dry run ok), 1 error, 2 NOT JOINED (no clear overlap).
"""

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

RATE = 4000                  # audio rate used for matching
TEMPLATE_SECONDS = 10        # length of the audio snippet that is searched for (overlap must be longer)
TEMPLATE_STARTS = (0, 30, 90)  # snippets taken this many seconds into file N+1
MIN_PEAK = 0.6               # best normalised correlation must be at least this
MIN_MARGIN = 0.2             # ...and beat the next-best candidate by this much
EXCLUDE_SECONDS = 0.5        # candidates closer than this to the best are the same match
DURATION_TOLERANCE = 2.0     # output length may differ from the plan by this many seconds


class JoinError(Exception):
    pass


class NotJoined(JoinError):
    pass


def require_tools():
    missing = [n for n in ("ffprobe", "ffmpeg", "nice") if not shutil.which(n)]
    if missing:
        raise JoinError("Required command not found: " + ", ".join(missing))
    try:
        import numpy  # noqa: F401
    except ImportError as exc:
        raise JoinError("Python package numpy is required (apt-get install python3-numpy).") from exc


def probe(path):
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "format=duration:stream=codec_type,codec_name,width,height,pix_fmt",
             "-of", "json", str(path)],
            capture_output=True, text=True, check=True).stdout
        data = json.loads(out)
        duration = float(data["format"]["duration"])
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError("duration is not positive")
    except (OSError, subprocess.SubprocessError, ValueError, KeyError) as exc:
        raise JoinError(f"Could not read {path}: {exc}") from exc
    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), {})
    audio = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
    if audio is None:
        raise JoinError(f"{path} has no audio track, so the overlap cannot be measured.")
    return {
        "duration": duration,
        "video": (video.get("codec_name"), video.get("width"), video.get("height"), video.get("pix_fmt")),
        "audio": audio.get("codec_name"),
        "has_video": bool(video),
    }


def decode_audio(path, start=None, seconds=None):
    """Mono 4 kHz signed 16-bit audio as a numpy array."""
    import numpy as np
    cmd = ["nice", "-n", "19", "ffmpeg", "-v", "error"]
    if start:
        cmd += ["-ss", str(start)]
    cmd += ["-i", str(path)]
    if seconds:
        cmd += ["-t", str(seconds)]
    cmd += ["-vn", "-ac", "1", "-ar", str(RATE), "-f", "s16le", "pipe:1"]
    try:
        raw = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise JoinError(f"ffmpeg could not decode the audio of {path}: {exc}") from exc
    return np.frombuffer(raw[: len(raw) // 2 * 2], dtype="<i2")


def normalized_correlation(x, template):
    """Normalised cross-correlation of template over every position of x."""
    import numpy as np
    n = len(template)
    if len(x) < n:
        return None
    t = template.astype(np.float64)
    t -= t.mean()
    t_norm = math.sqrt(float(np.dot(t, t)))
    if t_norm < 1e-9:
        return None
    lags_total = len(x) - n + 1
    result = np.zeros(lags_total, dtype=np.float32)
    block = 1 << 20
    for start in range(0, lags_total, block):
        lags = min(block, lags_total - start)
        seg = x[start:start + lags + n - 1].astype(np.float64)
        size = 1 << (lags + n - 2).bit_length()
        corr = np.fft.irfft(np.fft.rfft(seg, size) * np.conj(np.fft.rfft(t, size)), size)[:lags]
        c1 = np.concatenate(([0.0], np.cumsum(seg)))
        c2 = np.concatenate(([0.0], np.cumsum(seg * seg)))
        sums = c1[n:n + lags] - c1[:lags]
        var = c2[n:n + lags] - c2[:lags] - sums * sums / n
        ok = var > n  # quieter than about 1 LSB of noise: no information
        denom = t_norm * np.sqrt(np.where(ok, var, 1.0))
        result[start:start + lags] = np.where(ok, corr / denom, 0.0)
    return result


def best_and_competitor(ncc):
    import numpy as np
    index = int(np.argmax(ncc))
    peak = float(ncc[index])
    gap = int(EXCLUDE_SECONDS * RATE)
    parts = [ncc[:max(0, index - gap)], ncc[index + gap + 1:]]
    competitor = max((float(p.max()) for p in parts if len(p)), default=0.0)
    return index, peak, competitor


def find_start(a_audio, b_path, b_duration):
    """Seconds into file A at which file B begins, with peak and margin."""
    best_report = "file B is too short to take an audio sample from"
    n = TEMPLATE_SECONDS * RATE
    for start in TEMPLATE_STARTS:
        if start + TEMPLATE_SECONDS > b_duration:
            continue
        template = decode_audio(b_path, start, TEMPLATE_SECONDS)
        if len(template) < n * 0.95:
            continue
        ncc = normalized_correlation(a_audio, template)
        if ncc is None:
            best_report = "the audio is silent where it was sampled"
            continue
        index, peak, competitor = best_and_competitor(ncc)
        margin = peak - competitor
        best_report = f"peak correlation {peak:.3f}, margin over next-best match {margin:.3f}"
        if peak >= MIN_PEAK and margin >= MIN_MARGIN:
            offset = index / RATE - start
            if offset < -0.5:
                raise NotJoined("the second file starts BEFORE the first one; check the file order")
            return max(offset, 0.0), peak, margin
    raise NotJoined("no clear audio overlap (" + best_report + f"; need peak >= {MIN_PEAK} and margin >= {MIN_MARGIN})")


def plan_join(files):
    """Measure every consecutive pair. Returns (infos, offsets, details)."""
    infos = [probe(f) for f in files]
    offsets, details = [], []
    for i in range(len(files) - 1):
        a_audio = decode_audio(files[i])
        offset, peak, margin = find_start(a_audio, files[i + 1], infos[i + 1]["duration"])
        if offset > infos[i]["duration"] - 1:
            raise NotJoined(f"{files[i + 1].name} starts after {files[i].name} ends (gap, not overlap)")
        offsets.append(offset)
        details.append((files[i].name, files[i + 1].name, offset, infos[i]["duration"] - offset, peak, margin))
    return infos, offsets, details


def run_ffmpeg(args):
    cmd = ["nice", "-n", "19", "ffmpeg", "-v", "error", "-y"] + [str(a) for a in args]
    result = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    if result.returncode:
        raise JoinError("ffmpeg failed: " + result.stderr.strip()[-400:])


def concat_quote(path):
    return "file '" + str(path).replace("'", "'\\''") + "'"


def build(files, infos, offsets, out_path, copy_mode):
    keeps = list(offsets) + [None]
    out_path = Path(out_path)
    tmp_out = out_path.with_name(out_path.name + ".tmp")
    fmt = "mp4" if out_path.suffix.lower() == ".mp4" else "matroska"
    width, height = infos[-1]["video"][1], infos[-1]["video"][2]
    with tempfile.TemporaryDirectory(prefix="join_", dir=str(out_path.parent)) as work:
        segments = []
        for i, (f, keep) in enumerate(zip(files, keeps)):
            seg = Path(work) / f"seg{i}.mkv"
            args = ["-i", f]
            if keep is not None:
                args += ["-t", f"{keep:.3f}"]
            if copy_mode:
                args += ["-map", "0:v:0", "-map", "0:a:0", "-c", "copy"]
            else:
                args += ["-map", "0:v:0", "-map", "0:a:0",
                         "-vf", f"scale={width}:{height},setsar=1", "-pix_fmt", "yuv420p",
                         "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                         "-c:a", "aac", "-ac", "2", "-ar", "44100", "-b:a", "96k"]
            run_ffmpeg(args + [seg])
            segments.append(seg)
        listing = Path(work) / "list.txt"
        listing.write_text("\n".join(concat_quote(s) for s in segments) + "\n", encoding="utf-8")
        final = ["-f", "concat", "-safe", "0", "-i", listing, "-c", "copy"]
        if fmt == "mp4":
            final += ["-movflags", "+faststart"]
        run_ffmpeg(final + ["-f", fmt, tmp_out])
    expected = sum(offsets) + infos[-1]["duration"]
    try:
        actual = probe(tmp_out)["duration"]
    except JoinError:
        tmp_out.unlink(missing_ok=True)
        raise
    if abs(actual - expected) > DURATION_TOLERANCE:
        tmp_out.unlink(missing_ok=True)
        raise JoinError(f"Output is {actual:.1f} s but {expected:.1f} s was expected; nothing was written.")
    os.replace(tmp_out, out_path)
    return actual


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("files", nargs="+", type=Path)
    parser.add_argument("--dry-run", action="store_true", help="measure only; write nothing")
    parser.add_argument("--out", type=Path, help="output path (.mkv or .mp4)")
    args = parser.parse_args(argv)
    try:
        if len(args.files) < 2:
            raise JoinError("Give at least two files, oldest first.")
        for f in args.files:
            if not f.is_file():
                raise JoinError(f"Not a file: {f}")
        require_tools()
        infos, offsets, details = plan_join(args.files)
        copy_mode = all(i["video"] == infos[0]["video"] and i["audio"] == infos[0]["audio"]
                        and i["has_video"] for i in infos)
        expected = sum(offsets) + infos[-1]["duration"]
        for a, b, off, overlap, peak, margin in details:
            print(f"{b} starts {off:.2f} s into {a} (overlap {overlap:.1f} s, "
                  f"peak {peak:.3f}, margin {margin:.3f})")
        print(f"Mode: {'stream copy (lossless)' if copy_mode else 're-encode to H.264/AAC (slow)'}")
        print(f"Expected length: {expected:.1f} s")
        suffix = ".mkv" if copy_mode else ".mp4"
        out = args.out or args.files[-1].with_name(args.files[-1].stem + "_joined" + suffix)
        if args.dry_run:
            print("Dry run: nothing written.")
            return 0
        if out.exists():
            raise JoinError(f"{out} already exists; choose another --out.")
        need = int(sum(f.stat().st_size for f in args.files) * 2.2)
        free = shutil.disk_usage(out.resolve().parent).free
        if free < need:
            raise JoinError(f"Not enough free space ({free // 2**20} MB free, about {need // 2**20} MB needed).")
        actual = build(args.files, infos, offsets, out, copy_mode)
        print(f"Wrote {out} ({actual:.1f} s). Inputs were not changed.")
        return 0
    except NotJoined as exc:
        print(f"NOT JOINED: {exc}", file=sys.stderr)
        return 2
    except JoinError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
