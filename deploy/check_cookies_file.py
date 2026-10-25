#!/usr/bin/env python3
"""Check a Netscape cookies file without displaying cookie names or values."""

import re
import sys


def allowed_domain(domain):
    domain = domain.lower().lstrip(".").rstrip(".")
    fixed_domains = ("youtube.com", "google.com", "googlevideo.com")
    if any(domain == base or domain.endswith("." + base) for base in fixed_domains):
        return True

    # Google country domains include google.in and google.co.in/google.com.au.
    google_country_domain = re.fullmatch(r"google\.(?:[a-z]{2}|[a-z]{2,3}\.[a-z]{2})", domain)
    return bool(google_country_domain or re.fullmatch(
        r".+\.google\.(?:[a-z]{2}|[a-z]{2,3}\.[a-z]{2})", domain))


def main(argv):
    if len(argv) != 2:
        print("Usage: check_cookies_file.py <path>", file=sys.stderr)
        return 2

    path = argv[1]
    try:
        with open(path, encoding="utf-8", errors="replace") as cookie_file:
            lines = cookie_file.read().splitlines()
    except OSError as error:
        print(f"Could not read cookies file: {error}", file=sys.stderr)
        return 1

    if not lines or lines[0].rstrip("\r") != "# Netscape HTTP Cookie File":
        print("Invalid cookies file: first line must be # Netscape HTTP Cookie File", file=sys.stderr)
        return 1

    counts = {}
    offending = set()
    malformed = False
    for line in lines[1:]:
        if not line or (line.startswith("#") and not line.startswith("#HttpOnly_")):
            continue
        fields = line.split("\t")
        if len(fields) != 7:
            malformed = True
            continue
        domain = fields[0]
        if domain.startswith("#HttpOnly_"):
            domain = domain[len("#HttpOnly_"):]
        domain = domain.lower().lstrip(".").rstrip(".")
        if not domain:
            malformed = True
            continue
        counts[domain] = counts.get(domain, 0) + 1
        if not allowed_domain(domain):
            offending.add(domain)

    if malformed:
        print("Invalid cookies file: expected seven tab-separated fields per cookie row", file=sys.stderr)
        return 1
    if not counts:
        print("Invalid cookies file: no cookies found", file=sys.stderr)
        return 1
    if offending:
        print("Cookie domains outside the allowed YouTube/Google domains:", file=sys.stderr)
        for domain in sorted(offending):
            print(domain, file=sys.stderr)
        return 1

    for domain in sorted(counts):
        print(f"{domain}: {counts[domain]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
