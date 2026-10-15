"""从 校验-rNN.json 生成发布清单 latest.json —— 数字一律读出来，不手抄。

为什么要有这个：r119 之前那次检查发现服务器上的 latest.json 还写着 r109，而发货那版已经是
r118 —— 因为这份清单一直是**手工**造的，`check_update` 那条工具于是对每个买家说假话。
这个脚本只认两样东西：交付流水线自己写的 校验-rNN.json，和盘上那个 zip 的真实大小。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--receipt", type=Path, required=True, help="交付流水线写的 校验-rNN.json")
    parser.add_argument("--zip", type=Path, required=True, help="要发给买家的那个 zip")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--notes", required=True, help="给买家看的一段话")
    args = parser.parse_args()

    receipt = json.loads(args.receipt.read_text(encoding="utf-8"))
    if not receipt.get("ok"):
        print("校验回执本身就没通过，不出清单", file=sys.stderr)
        return 1
    if receipt.get("full_crc") is not True:
        print("回执里没有独立回读全 CRC 通过的记录，不出清单", file=sys.stderr)
        return 1

    # 回执说它是谁，盘上那个文件就得真是它 —— 不重算一遍就等于信了一张纸
    recomputed = sha256_of(args.zip)
    if recomputed != receipt["sha256"]:
        print(f"zip 实际哈希 {recomputed} 与回执 {receipt['sha256']} 不一致", file=sys.stderr)
        return 1
    size = args.zip.stat().st_size
    if abs(size - int(receipt["size"])) > 0:
        print(f"zip 实际大小 {size} 与回执 {receipt['size']} 不一致", file=sys.stderr)
        return 1

    manifest = {
        "round": receipt["round"],
        "size_mb": round(size / 1024 / 1024, 1),
        "sha256": receipt["sha256"],
        "exe_sha256": receipt["exe_sha256"],
        "source_sha256": receipt["source_sha256"],
        "built_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(receipt_zip_mtime(args.zip))),
        "notes": args.notes,
        "url": "",
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False)[:400])
    return 0


def receipt_zip_mtime(path: Path) -> float:
    return path.stat().st_mtime


if __name__ == "__main__":
    raise SystemExit(main())
