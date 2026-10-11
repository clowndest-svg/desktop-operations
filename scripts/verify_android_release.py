"""Verify a release APK's signer, actual version, native code, and built web assets."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import zipfile
from pathlib import Path

from release_manifest import digest_file, mobile_fingerprint, write_json

ROOT = Path(__file__).resolve().parents[1]


def command(args: list[str]) -> str:
    result = subprocess.run(
        args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=120,
    )
    if result.returncode:
        raise ValueError(result.stdout + result.stderr)
    return result.stdout


def signature(apk: Path, sdk: Path) -> str:
    java_home = os.environ.get("JAVA_HOME", "")
    java = str(Path(java_home) / "bin" / "java.exe") if java_home else "java"
    output = command(
        [
            java,
            "-jar",
            str(sdk / "build-tools/35.0.0/lib/apksigner.jar"),
            "verify",
            "--verbose",
            "--print-certs",
            str(apk),
        ]
    )
    found = re.search(r"Signer #1 certificate SHA-256 digest: ([0-9a-fA-F]+)", output)
    if found is None:
        raise ValueError("APK 未返回有效签名指纹")
    return found.group(1).lower()


def apk_version(apk: Path, sdk: Path) -> tuple[str, int, str]:
    output = command([str(sdk / "build-tools/35.0.0/aapt.exe"), "dump", "badging", str(apk)])
    found = re.search(
        r"package: name='([^']+)' versionCode='([0-9]+)' versionName='([^']+)'", output
    )
    if found is None:
        raise ValueError("无法读取 APK 的实际包名与版本")
    return found.group(1), int(found.group(2)), found.group(3)


def verify(apk: Path, sdk: Path, previous: Path | None = None) -> dict[str, object]:
    gradle = (ROOT / "frontend/android/app/build.gradle").read_text(encoding="utf-8")
    code = re.search(r"versionCode\s+([0-9]+)", gradle)
    version = re.search(r'versionName\s+"([^"\n]+)"', gradle)
    if code is None or version is None:
        raise ValueError("源码缺少版本号")
    package, actual_code, actual_version = apk_version(apk, sdk)
    if (package, actual_code, actual_version) != (
        "com.xiaoye.assistant",
        int(code.group(1)),
        version.group(1),
    ):
        raise ValueError("APK 实际版本和源码不一致")
    signer = signature(apk, sdk)
    if previous is not None:
        if signature(previous, sdk) != signer:
            raise ValueError("新旧 APK 签名不同，不能安全覆盖升级")
        if apk_version(previous, sdk)[1] >= actual_code:
            raise ValueError("versionCode 必须大于上一版")
    checked = 0
    with zipfile.ZipFile(apk) as archive:
        if archive.testzip() is not None:
            raise ValueError("APK CRC 校验失败")
        for path in sorted((ROOT / "frontend/dist-mobile").rglob("*")):
            if not path.is_file():
                continue
            member = "assets/public/" + path.relative_to(ROOT / "frontend/dist-mobile").as_posix()
            try:
                actual = hashlib.sha256(archive.read(member)).hexdigest()
            except KeyError as exc:
                raise ValueError(f"APK 缺少前端资源 {member}") from exc
            if actual != digest_file(path):
                raise ValueError(f"APK 前端资源过期 {member}")
            checked += 1
        if checked == 0:
            raise ValueError("没有前端资源可供核对")
        dex = b"".join(
            archive.read(name)
            for name in archive.namelist()
            if re.fullmatch(r"classes[0-9]*\.dex", name)
        )
        for marker in (
            b"XySecurePlugin",
            b"AES/GCM/NoPadding",
            b"XyNetPlugin",
            b"navigateTo",
            b"setTimer",
        ):
            if marker not in dex:
                raise ValueError(f"APK 缺少本轮原生能力 {marker.decode()}")
    newest_source = max(
        (
            path.stat().st_mtime
            for path in (ROOT / "frontend/android/app/src/main/java").rglob("*.java")
        ),
        default=0,
    )
    if newest_source > apk.stat().st_mtime:
        raise ValueError("APK 构建后原生源码又发生变化，请重新构建")
    return {
        "ok": True,
        "apk": str(apk.resolve()),
        "sha256": digest_file(apk),
        "bytes": apk.stat().st_size,
        "package": package,
        "version": actual_version,
        "version_code": actual_code,
        "signer_sha256": signer,
        "mobile_source_sha256": mobile_fingerprint(ROOT),
        "web_assets_verified": checked,
        "device_tested": False,
        "device_note": "未连接 Android 真机；编译/签名/静态内容验证不等于 ROM 验收",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apk", type=Path, required=True)
    parser.add_argument(
        "--sdk", type=Path, default=Path(os.environ.get("ANDROID_HOME", "D:/Android/Sdk"))
    )
    parser.add_argument("--previous", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = verify(args.apk, args.sdk, args.previous)
    except (OSError, ValueError, zipfile.BadZipFile, subprocess.TimeoutExpired) as exc:
        print(f"APK 校验失败：{exc}", file=sys.stderr)
        return 1
    write_json(args.report, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
