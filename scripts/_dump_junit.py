"""Dump the failing cases out of a pytest JUnit XML, one line each."""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET

tree = ET.parse(sys.argv[1] if len(sys.argv) > 1 else "/tmp/baseline.xml")
bad: list[tuple[str, str, str, str]] = []
for case in tree.iter("testcase"):
    for kind in ("failure", "error"):
        node = case.find(kind)
        if node is not None:
            message = (node.get("message") or "").replace("\n", " ")[:200]
            bad.append((case.get("classname") or "", case.get("name") or "", kind, message))

print(f"FAILURES: {len(bad)}")
for cls, name, kind, message in bad:
    print(f"{kind:8} {cls}::{name}")
    print(f"         {message}")
