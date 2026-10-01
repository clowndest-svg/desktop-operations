"""Run the HUD's own cleaner from the command line: scan, then optionally delete.

This is the same :class:`~jarvis.app.disk_service.DiskService` the 扫描 / 清理选中
buttons call, wired the way ``_run_desktop`` wires it -- same audit log path, same
protected data root -- so nothing about the safety model changes when it is driven
from here. ``--apply`` is what stands in for a human pressing 确认删除.

    python scripts/run_cleanup.py            # read-only scan, prints the plan
    python scripts/run_cleanup.py --apply    # delete everything the scan listed
"""

from __future__ import annotations

import argparse
import sys

from jarvis.app.disk_service import DiskService
from jarvis.config import ConfigService
from jarvis.tools.disk_cleaner import DiskCleaner

GIB = 1024**3
MIB = 1024**2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="扫描 C 盘垃圾；只有 --apply 才会删除")
    parser.add_argument("--apply", action="store_true", help="真的删除；不加这个参数只扫描")
    args = parser.parse_args(argv)

    config = ConfigService()
    config.start()
    paths = config.paths
    disk = DiskService(
        lambda: DiskCleaner(
            audit_log=paths.audit_dir / "deletions.jsonl",
            protected_dirs=(paths.data_dir,),
        )
    )
    disk.start()
    try:
        plan = disk.plan()
        entries = [item for group in plan.groups for item in group["items"]]
        print(f"数据根（受保护）  : {paths.data_dir}")
        print(f"审计日志          : {paths.audit_dir / 'deletions.jsonl'}")
        print(f"条目 / 字节       : {len(entries)} 项 / {plan.total_bytes / GIB:.2f} GiB")
        print(f"受保护而跳过      : {plan.skipped_protected}")
        print(f"被条目上限截断    : {plan.truncated}")
        if plan.error:
            print(f"扫描报错          : {plan.error}")
            return 1

        print("\n按类别：")
        for group in sorted(plan.groups, key=lambda entry: -int(entry["total_bytes"])):
            print(
                f"  {group['category']:<14} {len(group['items']):>5} 项  "
                f"{int(group['total_bytes']) / MIB:>10.1f} MiB"
            )

        print("\n最大的 12 项：")
        for item in sorted(entries, key=lambda entry: -int(entry["size_bytes"]))[:12]:
            print(f"  {int(item['size_bytes']) / MIB:>9.1f} MiB  {item['path']}")

        if not args.apply:
            print("\n只扫描。加 --apply 才会删。")
            return 0
        if not entries:
            print("\n没有可删的东西，不动。")
            return 0

        outcome = disk.clean(entries, confirmed=True)
        print(
            f"\n删除 {len(outcome.deleted)} 项，释放 {outcome.freed_bytes / GIB:.2f} GiB，"
            f"失败 {len(outcome.failed)} 项",
        )
        for entry in list(outcome.failed)[:8]:
            print(f"  失败：{entry}")
        if outcome.error:
            print(f"  错误：{outcome.error}")
        print(f"审计记录：{outcome.log_path}")
        return 0 if not outcome.error else 1
    finally:
        disk.stop()
        config.stop()


if __name__ == "__main__":
    sys.exit(main())
