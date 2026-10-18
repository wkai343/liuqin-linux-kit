#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Validate local links in maintained docs; leave third-party notices intact."""
from pathlib import Path
import re
import sys
from urllib.parse import unquote, urlsplit

PROJECT = Path(__file__).resolve().parents[1]

def broken_links():
    paths = list(PROJECT.glob('*.md')) + list((PROJECT/'docs').glob('*.md'))
    paths += [PROJECT/name/'README.md' for name in ('build', 'installer', 'shared', 'tests')]
    failures = []
    for source in paths:
        if not source.is_file():
            continue
        # Examples in code fences are not navigation links.
        content = re.sub(r'```[^\n]*\n[\s\S]*?```', '', source.read_text())
        for match in re.finditer(r'\[[^\]]*\]\(([^)]+)\)', content):
            target = match.group(1).strip().split()[0].strip('<>')
            url = urlsplit(target)
            if url.scheme or url.netloc or not url.path:
                continue
            path = source.parent/unquote(url.path)
            if not path.exists():
                line = content.count('\n', 0, match.start()) + 1
                failures.append(f'{source.relative_to(PROJECT)}:{line}: {target}')
    return failures

if __name__ == '__main__':
    failures = broken_links()
    if failures:
        print('\n'.join(failures), file=sys.stderr)
        sys.exit(1)
    print('PASS: maintained documentation links resolve')
