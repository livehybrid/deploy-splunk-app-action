#!/usr/bin/env python3
"""Fail if a built Splunk app bundles a certifi that still trusts eMudhra's compromised root.

In October 2026 the private keys of eight eMudhra intermediate CAs leaked
(https://emudhra.we-have-a-problem.com/). certifi 2026.10.18 dropped every
emSign root; 2026.10.19 restored the three unaffected ones and keeps
"emSign ECC Root CA - G3" removed. An add-on that vendors certifi into lib/
keeps trusting whatever bundle it was built with, so the check runs on the
built package, not on the requirements that were asked for.

Usage: check_bundled_certifi.py <built app dir> [<more dirs>...]
Apps that bundle no certifi pass.
"""
import pathlib
import re
import sys

MIN_VERSION = (2026, 10, 19)
DISTRUSTED = ["emSign ECC Root CA - G3"]


def version_of(certifi_dir):
    init = certifi_dir / "__init__.py"
    m = re.search(r"""__version__\s*=\s*["']([0-9.]+)["']""", init.read_text(errors="replace")) if init.exists() else None
    if not m:
        return None, "unknown"
    return tuple(int(p) for p in m.group(1).split(".")), m.group(1)


def check(roots):
    problems, seen = [], 0
    for root in roots:
        for pem in sorted(pathlib.Path(root).rglob("certifi/cacert.pem")):
            seen += 1
            ver, text = version_of(pem.parent)
            labels = set(re.findall(r'^# Label: "([^"]+)"', pem.read_text(errors="replace"), re.M))
            bad = [d for d in DISTRUSTED if d in labels]
            print(f"{pem}: certifi {text}, {len(labels)} roots")
            if bad:
                problems.append(f"{pem}: still trusts {', '.join(bad)}")
            if ver is None or ver < MIN_VERSION:
                problems.append(f"{pem}: certifi {text} is older than "
                                f"{'.'.join(map(str, MIN_VERSION))}; pin certifi>=2026.10.19 in lib/requirements.txt")
    if not seen:
        print("no bundled certifi found: nothing to check")
    return problems


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    found = check(sys.argv[1:])
    if found:
        sys.exit("bundled certifi is not safe:\n  " + "\n  ".join(found))
