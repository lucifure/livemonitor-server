# LiveMonitor server

The server records live YouTube streams and provides an authenticated API for the Android app. See [deploy/README.md](deploy/README.md) for VM setup and deployment notes.

## Join overlapping recordings

When one stream was saved as multiple files that overlap in time, join them in recording order (oldest first):

```sh
python3 tools/join_recordings.py first.mkv second.mp4 [third.mp4 ...]
python3 tools/join_recordings.py first.mkv second.mp4 --dry-run
python3 tools/join_recordings.py first.mkv second.mp4 --out /path/to/joined.mp4
```

The tool needs `ffmpeg`, `ffprobe`, `nice`, and Python `numpy`. It checks audio overlap, keeps the opening from the first file, trims transitions near the middle of each overlap, and encodes a new MP4. Inputs are never changed. A dry run only reports pair offsets and the expected output duration. An overlap is accepted only when normalized peak correlation is at least 0.25 and at least 8 robust standard deviations above the median background correlation; otherwise it exits with code 2 without creating an output.

Audio is decoded in 64 KiB chunks and reduced to one RMS-change fingerprint per 40 ms. Fingerprints use about 6 KB per minute per input (about 720 KB for a two-hour file), plus temporary FFT working arrays that can use several dozen MB for multi-hour recordings. Video is decoded and re-encoded by ffmpeg rather than loaded into Python memory.
