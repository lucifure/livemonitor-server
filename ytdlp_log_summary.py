"""Short plain-text summary of one yt-dlp debug log.

The server calls summarize_ytdlp_log() when a recording ends and writes the
returned lines into the normal server log, so the facts needed to understand
why yt-dlp stopped travel with the server log and nobody has to copy the big
debug file around.  Reads the file line by line; never raises for a missing or
unreadable file (it returns one explanatory line instead).
"""
import os
import re
from collections import Counter, deque

TIMESTAMP = re.compile(r"^\[(\d{4}-\d\d-\d\d) (\d\d:\d\d:\d\d)\] ?")
SKIPPED = re.compile(r"Skipping fragment (\d+)")
PLAYABILITY = re.compile(r"(\S+) player response playability status: (\w+)")
VERSION = re.compile(r"yt-dlp version (\S+)")
FORMATS = re.compile(r"Downloading \d+ format\(s\): (\S+)")
SECRET = re.compile(r"(?i)\b(token|sig|signature|key|cookie|cookies|auth|authorization|password|pot|po_token)=\S+")
QUERY = re.compile(r"\?\S+")


def _mask(text):
    """Remove URL query strings and key=value secrets from a log line."""
    return QUERY.sub("?...", SECRET.sub(r"\1=***", text))


def _clip(text, width):
    return text if len(text) <= width else text[:width - 3] + "..."


def _label(total, shown):
    """Count goes before the list so clipping a long line never hides it."""
    return f"{total}" if total == shown else f"{total}, first {shown}"


def summarize_ytdlp_log(path, tail_lines=12, max_items=12, width=150):
    name = os.path.basename(str(path))
    try:
        handle = open(path, "r", errors="replace")
    except OSError as error:
        return [f"ytdlp-summary: cannot read {name}: {error}"]

    counts = Counter()
    lines = 0
    first_time = last_time = None
    version = formats = None
    skipped = []       # (time, fragment)
    playability = []   # (time, client, status)
    fetches = []       # times of "Downloading webpage"
    recent = deque(maxlen=tail_lines)
    before_done = None

    with handle:
        for raw in handle:
            lines += 1
            text = raw.rstrip("\r\n")
            stamp = TIMESTAMP.match(text)
            when = stamp.group(2) if stamp else "?"
            body = text[stamp.end():] if stamp else text
            if stamp:
                first_time = first_time or when
                last_time = when
            if version is None:
                found = VERSION.search(body)
                if found:
                    version = found.group(1)
            if formats is None:
                found = FORMATS.search(body)
                if found:
                    formats = found.group(1)
            found = SKIPPED.search(body)
            if found:
                counts["skipped fragments"] += 1
                skipped.append((when, found.group(1)))
            found = PLAYABILITY.search(body)
            if found:
                playability.append((when, found.group(1), found.group(2)))
            if "Downloading webpage" in body:
                fetches.append(when)
            lowered = body.lower()
            if "not found" in lowered:
                counts["not found"] += 1
            if "retrying" in lowered:
                counts["retrying"] += 1
            if "http error" in lowered:
                counts["http errors"] += 1
            if "fragment request failed" in lowered:
                counts["request failed"] += 1
            if "error" in lowered and "[debug]" not in body:
                counts["errors"] += 1
            if "warning" in lowered:
                counts["warnings"] += 1
            if body.strip():
                recent.append(f"{when} {_mask(body.strip())}")
            if before_done is None and "[download] 100%" in body:
                before_done = list(recent)[:-1]

    out = [f"ytdlp-summary {name}: {lines} lines, log times {first_time or '?'} to {last_time or '?'}"]
    out.append(f"ytdlp-summary: yt-dlp {version or '?'}, formats {formats or '?'}")
    count_text = ", ".join(f"{key} {counts[key]}" for key in
                           ("skipped fragments", "not found", "retrying", "http errors",
                            "request failed", "errors", "warnings"))
    out.append(f"ytdlp-summary: counts: {count_text}")
    if skipped:
        shown = skipped[:max_items]
        out.append(f"ytdlp-summary: skipped fragments ({_label(len(skipped), len(shown))}): "
                   + ", ".join(f"{frag}@{when}" for when, frag in shown))
    else:
        out.append("ytdlp-summary: skipped fragments (0): none")
    if playability:
        tally = Counter(status for _when, _client, status in playability)
        out.append("ytdlp-summary: playability: "
                   + ", ".join(f"{n}x {status}" for status, n in tally.most_common()))
        shown = playability[-max_items:]
        out.append(f"ytdlp-summary: latest playability (last {len(shown)}): "
                   + ", ".join(f"{when} {client}={status}" for when, client, status in shown))
    else:
        out.append("ytdlp-summary: playability: no lines")
    if fetches:
        out.append(f"ytdlp-summary: webpage fetches {len(fetches)}, latest: " + ", ".join(fetches[-max_items:]))
    else:
        out.append("ytdlp-summary: webpage fetches 0")
    if before_done is not None:
        out.append("ytdlp-summary: last lines before the download finished:")
        tail = before_done
    else:
        out.append("ytdlp-summary: last lines of the log:")
        tail = list(recent)
    out.extend("ytdlp-summary |   " + line for line in tail)
    return [_clip(line, width) for line in out]
