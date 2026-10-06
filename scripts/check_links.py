"""Fail when a relative link in a tracked Markdown file points at a missing file.

Usage: uv run python scripts/check_links.py   (from anywhere inside the repository)

Links with a URL scheme, pure #anchors and anything in code (fenced or inline) are
skipped; anchors are not checked.
"""

import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote

INLINE = re.compile(r"!?\[(?:[^\]\[]|\[[^\]]*\])*\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
REFERENCE = re.compile(r"^\s*\[[^\]]+\]:\s*<?(\S+?)>?(?:\s|$)")
SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")


def targets(markdown: str):
    """(line number, link target) for every link outside code."""
    fenced = False
    for number, line in enumerate(markdown.splitlines(), 1):
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
            continue
        if fenced:
            continue
        text = re.sub(r"`[^`]*`", "", line)
        for target in INLINE.findall(text) + REFERENCE.findall(text):
            if not SCHEME.match(target) and not target.startswith("#"):
                yield number, target


def main() -> int:
    root = Path(
        subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True
        ).stdout.strip()
    )
    files = subprocess.run(
        ["git", "-C", str(root), "ls-files", "*.md"], capture_output=True, text=True, check=True
    ).stdout.split()
    checked = broken = 0
    for name in files:
        path = root / name
        for number, target in targets(path.read_text(encoding="utf-8")):
            checked += 1
            relative = unquote(target.split("#", 1)[0].split("?", 1)[0])
            if not (path.parent / relative).exists():
                broken += 1
                print(f"{name}:{number}: broken link {target}")
    print(f"{checked} relative links in {len(files)} Markdown files, {broken} broken")
    return 1 if broken else 0


if __name__ == "__main__":
    sys.exit(main())
