"""Stops this machine's secrets and identity from reaching a commit or a push.

    python tools/secret_scan.py --staged           # what `git commit` would record
    python tools/secret_scan.py --range OLD..NEW   # what `git push` would publish

Two kinds of check, both on ADDED lines only:

  1. Values that exist on this machine: every long string in the user's
     credential files (api_keys.json, google_token.json), the email and name
     they hold, this machine's home path, hostname and git email. A leak of
     these is a leak of the real thing, so they are matched exactly.
  2. Shapes of secrets: private keys and the token formats of the services the
     eagle talks to.

A file that is never publishable (a key, an .env, the user's memory store) is
refused by name. Exit 1 blocks the commit or push.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import socket
import subprocess
import sys
from pathlib import Path

REFUSED_NAMES = [
    ".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "*.jks", "id_rsa*",
    "api_keys.json", "google_token.json", "*token*.json", "credentials*.json",
    "long_term*", "*.bak", "journal.jsonl", "eagle.log",
]
ALLOWED_NAMES = [".env.example"]

SHAPES = {
    "private key": r"BEGIN [A-Z ]*PRIVATE KEY",
    "Google API key": r"(?<![A-Za-z0-9_\-])AIza[0-9A-Za-z_\-]{35}(?![A-Za-z0-9_\-])",
    "AWS key": r"(?<![A-Za-z0-9+/])AKIA[0-9A-Z]{16}(?![A-Za-z0-9+/=])",
    "OpenAI/Anthropic key": r"(?<![A-Za-z0-9])sk-(?:ant-|proj-)?[A-Za-z0-9_\-]{24,}",
    "GitHub token": r"gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{20,}",
    "Slack token": r"xox[abprs]-[A-Za-z0-9\-]{10,}",
    "JWT": r"eyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}",
    "credential assignment": (
        r"(?i)(api[_-]?key|secret|token|passw(?:or)?d|client_secret)[\"']?\s*[:=]"
        r"\s*[\"'][A-Za-z0-9_\-\./+=]{20,}[\"']"),
}
_SHAPES = {k: re.compile(v) for k, v in SHAPES.items()}


def _data_dir() -> Path:
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from core.user_paths import user_data_dir
        return user_data_dir()
    except Exception:
        return Path.home() / ".local" / "share" / "aethelark"


def _strings(node):
    if isinstance(node, dict):
        for v in node.values():
            yield from _strings(v)
    elif isinstance(node, list):
        for v in node:
            yield from _strings(v)
    elif isinstance(node, str):
        yield node


def machine_values(data_dir: Path | None = None) -> dict[str, str]:
    """{value: what it is}, for everything on this machine that must not leave it."""
    found: dict[str, str] = {}
    data = data_dir or _data_dir()
    for name in ("api_keys.json", "google_token.json"):
        try:
            doc = json.loads((data / "config" / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for s in _strings(doc):
            if s.startswith(("http://", "https://")):
                continue    # OAuth scope URLs: public constants, also in the source
            if len(s) >= 16 or "@" in s:
                found[s] = f"a value from your {name}"
        for key in ("user_name", "name"):
            v = doc.get(key) if isinstance(doc, dict) else None
            if isinstance(v, str) and len(v) >= 4:
                found[v] = f"your name from {name}"
    for var, val in os.environ.items():
        if re.search(r"(KEY|TOKEN|SECRET|PASSWORD)$", var) and len(val) >= 16:
            found[val] = f"the environment variable {var}"
    home = str(Path.home())
    found[home + "/"] = "your home directory path"
    host = socket.gethostname()
    if len(host) >= 4:
        found[host] = "this machine's hostname"
    try:
        email = subprocess.run(["git", "config", "user.email"], capture_output=True,
                               text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        email = ""
    if email and "noreply" not in email:
        found[email] = "your git email"
    return found


def _refused(path: str) -> bool:
    base = os.path.basename(path)
    if any(fnmatch.fnmatch(base, p) for p in ALLOWED_NAMES):
        return False
    return any(fnmatch.fnmatch(base, p) for p in REFUSED_NAMES)


def added_lines(diff: str):
    """(path, line) for each added line of a unified diff."""
    path = None
    for raw in diff.split("\n"):
        if raw.startswith("+++ "):
            path = raw[6:] if raw.startswith("+++ b/") else None
        elif raw.startswith("+") and not raw.startswith("+++") and path:
            yield path, raw[1:]


def scan(diff: str, values: dict[str, str], changed: list[str]) -> list[str]:
    problems = [f"{p}: a file that is never published (by its name)"
                for p in changed if _refused(p)]
    for path, line in added_lines(diff):
        if "base64," in line:
            continue
        for value, what in values.items():
            if value in line:
                problems.append(f"{path}: contains {what}")
        for label, rx in _SHAPES.items():
            if rx.search(line):
                problems.append(f"{path}: looks like a {label}")
    return sorted(set(problems))


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True,
                          errors="replace").stdout


def main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[0] == "--range":
        rng = argv[1]
        diff = _git("diff", "--unified=0", "--no-color", rng)
        changed = _git("diff", "--name-only", "--diff-filter=AM", rng).split("\n")
    else:
        diff = _git("diff", "--cached", "--unified=0", "--no-color")
        changed = _git("diff", "--cached", "--name-only", "--diff-filter=AM").split("\n")
    problems = scan(diff, machine_values(), [c for c in changed if c])
    if not problems:
        return 0
    print("Blocked: this would publish something private.\n", file=sys.stderr)
    for p in problems:
        print(f"  {p}", file=sys.stderr)
    print("\nRemove it and try again. Nothing was committed or pushed.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
