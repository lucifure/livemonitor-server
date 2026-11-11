#!/usr/bin/env python3
"""Join overlapping recordings of one stream without changing the inputs."""

import argparse
import math
import os
import shutil
import subprocess
import sys
from pathlib import Path


RATE = 4000
FEATURE_HZ = 25  # one RMS feature for each 40 ms of decoded audio
MIN_OVERLAP_SECONDS = 10
MIN_PEAK_CORRELATION = 0.25
MIN_CONFIDENCE = 8.0


class JoinError(Exception):
    pass


def require_tools():
    missing = [name for name in ("ffprobe", "ffmpeg", "nice") if not shutil.which(name)]
    if missing:
        raise JoinError("Required command not found: " + ", ".join(missing))
    try:
        import numpy  # noqa: F401
    except ImportError as exc:
        raise JoinError("Python package numpy is required (install numpy to use this tool).") from exc


def probe_duration(path):
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, check=True)
        duration = float(result.stdout.strip())
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError("duration is not positive")
        return duration
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise JoinError(f"Could not read duration for {path}: {exc}") from exc


def decode_features(path):
    """Decode in chunks and retain only 25 Hz RMS samples (~100 bytes/minute)."""
    import numpy as np

    command = ["nice", "-n", "19", "ffmpeg", "-v", "error", "-i", str(path),
               "-vn", "-ac", "1", "-ar", str(RATE), "-f", "s16le", "pipe:1"]
    try:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError as exc:
        raise JoinError(f"Could not start ffmpeg for {path}: {exc}") from exc
    features = []
    remainder = b""
    samples_per_feature = RATE // FEATURE_HZ
    # communicate() would retain the full raw audio. Read bounded chunks instead.
    assert process.stdout is not None
    while True:
        chunk = process.stdout.read(65536)
        if not chunk:
            break
        chunk = remainder + chunk
        usable = len(chunk) - (len(chunk) % (samples_per_feature * 2))
        if usable:
            samples = np.frombuffer(chunk[:usable], dtype="<i2").reshape(-1, samples_per_feature)
            values = samples.astype(np.float32)
            # RMS of sample-to-sample change is sensitive to the signal's
            # changing pitch while remaining resilient to codec phase shifts.
            changes = np.diff(values, axis=1)
            features.append(np.sqrt(np.mean(changes * changes, axis=1)))
        remainder = chunk[usable:]
    code = process.wait()
    if code:
        raise JoinError(f"ffmpeg could not decode {path} (exit code {code})")
    if not features:
        raise JoinError(f"ffmpeg decoded no audio from {path}")
    return np.concatenate(features)


def find_overlap(a, b):
    """Return B's start in A and confidence using normalized FFT correlation.

    Confidence is the peak's robust z-score above the median, excluding a
    one-second neighborhood around the peak. Require z >= 8 and correlation
    >= 0.25, which rejects broad/ambiguous or weak matches.
    """
    import numpy as np

    a = np.asarray(a, dtype=np.float32)
    b = np.asarray(b, dtype=np.float32)
    a = a - np.mean(a)
    b = b - np.mean(b)
    min_samples = MIN_OVERLAP_SECONDS * FEATURE_HZ
    if len(a) < min_samples or len(b) < min_samples:
        raise JoinError("files are too short to establish a clear overlap")

    fft_size = 1 << (len(a) + len(b) - 2).bit_length()
    correlation = np.fft.irfft(np.fft.rfft(a, fft_size) * np.conj(np.fft.rfft(b, fft_size)), fft_size)
    # Positive lag means B starts later than A. Normalize each candidate by
    # the energy in the actual overlapping portions using cumulative sums.
    a_sq = np.concatenate(([0.0], np.cumsum(a * a, dtype=np.float64)))
    b_sq = np.concatenate(([0.0], np.cumsum(b * b, dtype=np.float64)))
    max_lag = len(a) - min_samples
    lags = np.arange(max_lag + 1)
    overlap = np.minimum(len(a) - lags, len(b))
    valid = overlap >= min_samples
    lags, overlap = lags[valid], overlap[valid]
    # correlation[lag] covers A[lag:] against B[:overlap] when B is shorter;
    # when A is shorter, overlap clipping is handled by the same energy terms.
    dot = correlation[lags]
    energy_a = a_sq[lags + overlap] - a_sq[lags]
    energy_b = b_sq[overlap]
    denom = np.sqrt(energy_a * energy_b)
    normalized = np.divide(dot, denom, out=np.zeros_like(dot), where=denom > 0)
    if not len(normalized):
        raise JoinError("files do not overlap for at least 10 seconds")
    best_index = int(np.argmax(normalized))
    peak = float(normalized[best_index])
    exclusion = FEATURE_HZ
    background = np.concatenate((normalized[:max(0, best_index - exclusion)],
                                 normalized[min(len(normalized), best_index + exclusion + 1):]))
    if not len(background):
        background = normalized
    median = float(np.median(background))
    mad = float(np.median(np.abs(background - median)))
    confidence = (peak - median) / max(1.4826 * mad, 1e-6)
    if peak < MIN_PEAK_CORRELATION or confidence < MIN_CONFIDENCE:
        raise JoinError(f"no clear overlap (peak correlation {peak:.3f}, confidence {confidence:.1f}; "
                        f"need >= {MIN_PEAK_CORRELATION:.2f} and >= {MIN_CONFIDENCE:.0f})")
    offset = float(lags[best_index]) / FEATURE_HZ
    overlap_seconds = float(min(len(a) / FEATURE_HZ - offset, len(b) / FEATURE_HZ))
    if overlap_seconds < MIN_OVERLAP_SECONDS:
        raise JoinError("files do not overlap for at least 10 seconds")
    return offset, overlap_seconds, confidence


def analyze(paths):
    durations = [probe_duration(path) for path in paths]
    features = [decode_features(path) for path in paths]
    pairs = []
    for index in range(len(paths) - 1):
        try:
            offset, overlap, confidence = find_overlap(features[index], features[index + 1])
        except JoinError as exc:
            raise JoinError(f"NOT JOINED: {paths[index].name} -> {paths[index + 1].name}: {exc}") from exc
        pairs.append((offset, overlap, confidence))
    return durations, pairs


def timeline_segments(durations, pairs):
    starts = [0.0] * len(durations)
    ends = list(durations)
    for index, (offset, overlap, _) in enumerate(pairs):
        cut = offset + overlap / 2.0
        ends[index] = min(ends[index], cut)
        starts[index + 1] = max(starts[index + 1], overlap / 2.0)
    segments = [(max(0.0, start), min(duration, end))
                for start, end, duration in zip(starts, ends, durations)]
    if any(end <= start for start, end in segments):
        raise JoinError("NOT JOINED: overlap cuts leave an empty file segment")
    return segments


def encode(paths, segments, output, expected_duration):
    if Path(output).resolve() in {Path(path).resolve() for path in paths}:
        raise JoinError("Output path must not be one of the input files")
    tmp = Path(str(output) + ".tmp")
    command = ["nice", "-n", "19", "ffmpeg", "-v", "error", "-y"]
    for path in paths:
        command += ["-i", str(path)]
    filters = []
    for index, (start, end) in enumerate(segments):
        filters.append(f"[{index}:v:0]trim=start={start:.3f}:end={end:.3f},setpts=PTS-STARTPTS[v{index}]")
        filters.append(f"[{index}:a:0]atrim=start={start:.3f}:end={end:.3f},asetpts=PTS-STARTPTS[a{index}]")
    concat_inputs = "".join(f"[v{i}][a{i}]" for i in range(len(paths)))
    filters.append(f"{concat_inputs}concat=n={len(paths)}:v=1:a=1[outv][outa]")
    command += ["-filter_complex", ";".join(filters), "-map", "[outv]", "-map", "[outa]",
                "-c:v", "libx264", "-crf", "23", "-preset", "veryfast", "-c:a", "aac",
                "-movflags", "+faststart", "-f", "mp4", str(tmp)]
    try:
        subprocess.run(command, check=True)
        actual = probe_duration(tmp)
        if abs(actual - expected_duration) > 2:
            raise JoinError(f"Output duration {actual:.2f}s is not within 2s of expected {expected_duration:.2f}s")
        os.replace(tmp, output)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    return actual


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", nargs="+", type=Path, metavar="FILE")
    parser.add_argument("--dry-run", action="store_true", help="measure overlaps without writing output")
    parser.add_argument("--out", type=Path, help="output path (default: last file stem + _joined.mp4)")
    args = parser.parse_args(argv)
    if len(args.files) < 2:
        parser.error("provide at least two input files")
    try:
        require_tools()
        for path in args.files:
            if not path.is_file():
                raise JoinError(f"Input file does not exist: {path}")
        durations, pairs = analyze(args.files)
        segments = timeline_segments(durations, pairs)
        expected = sum(end - start for start, end in segments)
        for index, (offset, overlap, confidence) in enumerate(pairs):
            print(f"{args.files[index].name} -> {args.files[index + 1].name}: "
                  f"B offset {offset:.2f}s, overlap {overlap:.2f}s, confidence {confidence:.1f}")
        print(f"Expected final duration: {expected:.2f}s")
        if args.dry_run:
            return 0
        output = args.out or args.files[-1].with_name(args.files[-1].stem + "_joined.mp4")
        actual = encode(args.files, segments, output, expected)
        print(f"Joined output: {output} ({actual:.2f}s); offsets used: " +
              ", ".join(f"{pair[0]:.2f}s" for pair in pairs))
        return 0
    except JoinError as exc:
        print(str(exc), file=sys.stderr)
        return 2 if str(exc).startswith("NOT JOINED:") else 1
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"Join failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
