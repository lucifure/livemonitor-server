import subprocess
import sys
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CHECKER = ROOT / "deploy" / "check_cookies_file.py"
HEADER = "# Netscape HTTP Cookie File\n"
FALLBACK_TEST = ROOT / "deploy" / "vm-fallback-test.sh"


def test_vm_fallback_uses_configured_ytdlp_path():
    script = FALLBACK_TEST.read_text(encoding="utf-8")
    assert 'YTDLP=/home/ubuntu/.local/bin/yt-dlp' in script
    assert 'timeout 40 "$YTDLP"' in script
    assert re.search(r"(?m)^\s*(?:timeout\s+\S+\s+)?yt-dlp\b", script) is None


def cookie_row(domain, name="PRIVATE_COOKIE_NAME", value="PRIVATE_COOKIE_VALUE"):
    return "\t".join((domain, "TRUE", "/", "TRUE", "0", name, value)) + "\n"


def check_file(tmp_path, content):
    path = tmp_path / "cookies.txt"
    path.write_text(content, encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(CHECKER), str(path)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_valid_youtube_and_google_cookie_domains_pass_without_secrets(tmp_path):
    result = check_file(tmp_path, HEADER + cookie_row(".youtube.com") + cookie_row(".google.co.in"))
    assert result.returncode == 0
    assert "youtube.com: 1" in result.stdout
    assert "google.co.in: 1" in result.stdout
    assert "PRIVATE_COOKIE_NAME" not in result.stdout + result.stderr
    assert "PRIVATE_COOKIE_VALUE" not in result.stdout + result.stderr


def test_claude_domain_fails_and_is_listed(tmp_path):
    result = check_file(tmp_path, HEADER + cookie_row("claude.ai"))
    assert result.returncode == 1
    assert "claude.ai" in result.stderr
    assert "PRIVATE_COOKIE_NAME" not in result.stdout + result.stderr
    assert "PRIVATE_COOKIE_VALUE" not in result.stdout + result.stderr


def test_missing_header_fails(tmp_path):
    result = check_file(tmp_path, cookie_row("youtube.com"))
    assert result.returncode == 1
    assert "first line must be # Netscape HTTP Cookie File" in result.stderr


def test_empty_file_fails(tmp_path):
    result = check_file(tmp_path, "")
    assert result.returncode == 1


def test_httponly_domain_prefix_is_accepted(tmp_path):
    result = check_file(tmp_path, HEADER + cookie_row("#HttpOnly_.youtube.com"))
    assert result.returncode == 0
    assert "youtube.com: 1" in result.stdout
