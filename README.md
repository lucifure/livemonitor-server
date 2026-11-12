# LiveMonitor server

The server records live YouTube streams and provides an authenticated API for the Android app. See [deploy/README.md](deploy/README.md) for VM setup and deployment notes.

## Join overlapping recordings

When one stream was saved as several files that overlap in time (darth_mb streams: the first file ends after about 10 minutes, the next one starts a few minutes before that point), join them in recording order, oldest first:

```sh
python3 tools/join_recordings.py first.mkv second.mkv [third.mkv ...] --dry-run
python3 tools/join_recordings.py first.mkv second.mkv [third.mkv ...]
python3 tools/join_recordings.py first.mkv second.mkv --out /path/to/joined.mkv
```

Needs `ffmpeg`, `ffprobe`, `nice` and Python `numpy` (`sudo apt-get install -y python3-numpy`).

How it works: it takes a 10 second audio sample from the start of each later file and finds where it sits inside the previous file (normalised waveform correlation at 4 kHz). The match must have a correlation of at least 0.6 and beat the next-best candidate by at least 0.2, otherwise it prints `NOT JOINED: ...`, exits with code 2 and writes nothing. It keeps each file up to the point where the next one starts, so the opening of the first file is always kept.

- If all files use the same video codec, picture size and audio codec, the join is a lossless stream copy into `.mkv` (takes seconds; the seam can be off by about one video frame).
- Otherwise every part is re-encoded to H.264/AAC into `.mp4` (slow).

`--dry-run` only measures and prints the offsets, the mode and the expected length. Input files are never changed, an existing output is never overwritten, and the output is checked against the expected length before it is renamed into place. The overlap must be longer than the 10 second sample. Memory: the audio of each earlier file is held at 4 kHz (about 0.5 MB per minute) plus temporary working arrays; a 2 hour 40 minute earlier file needs roughly 400 MB.
