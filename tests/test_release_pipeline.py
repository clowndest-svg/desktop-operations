"""Real tiny archives and isolated release trees exercise the production release tooling."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from scripts.release_manifest import (
    build_info,
    round_from,
    scoped_path,
    source_fingerprint,
    valid_round,
)

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def delivery(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location(
        "delivery_audit_test", ROOT / "scripts/delivery_package.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_package_name_promises_only_what_is_inside(delivery: ModuleType) -> None:
    """包名是一种承诺：不带 --with-mobile 就不许写「+手机」。

    r122 真踩到过 —— 手机端 APK 版本和源码不一致（盘上 0.4.2/10，build.gradle 已改到
    0.5.0/11）只能先出电脑端，结果 10.3 GB 的包顶着「小夜-电脑+手机-r122.zip」，
    清单和发货记录也跟着印这个名字，而包内 APK 成员数是 **0**。
    正反两半都要钉：只断言"不含手机"的话，判据会退化成把合并包也改名还全绿。
    """
    assert delivery.package_name("r122", True) == "小夜-电脑+手机-r122.zip"
    alone = delivery.package_name("r122", False)
    assert alone == "小夜-电脑-r122.zip", alone
    assert "手机" not in alone, alone


def test_newest_round_is_numeric_not_a_timestamp(tmp_path: Path, delivery: ModuleType) -> None:
    for name, stamp in (("r9", 200), ("r10", 100)):
        exe = tmp_path / name / "dist/小夜/小夜.exe"
        exe.parent.mkdir(parents=True)
        exe.write_bytes(b"test")
        os.utime(exe, (stamp, stamp))
    assert delivery.newest_round(tmp_path) == "r10"


@pytest.mark.parametrize("name", ["../r1", "r1/../../", "r1 & exit", "auto", "", "r-1"])
def test_round_cannot_escape_the_build_root(name: str) -> None:
    with pytest.raises(ValueError):
        valid_round(name)


def test_release_path_cannot_be_root_or_parent(tmp_path: Path) -> None:
    for path in (tmp_path, tmp_path / ".." / "outside"):
        with pytest.raises(ValueError):
            scoped_path(tmp_path, path)


def test_source_fingerprint_ignores_timestamp_but_not_content(tmp_path: Path) -> None:
    file = tmp_path / "jarvis/a.py"
    file.parent.mkdir()
    file.write_bytes(b"a = 1\n")
    before = source_fingerprint(tmp_path)
    file.write_bytes(b"a = 1\r\n")
    assert source_fingerprint(tmp_path) == before
    file.write_bytes(b"a = 2\n")
    os.utime(file, (1, 1))
    assert source_fingerprint(tmp_path) != before


@pytest.fixture()
def package_tree(
    tmp_path: Path, delivery: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> dict[str, Any]:
    source = tmp_path / "source"
    source.mkdir()
    exe = source / "小夜.exe"
    exe.write_bytes(b"executable fixture")
    models = source / "模型"
    models.mkdir()
    weight = models / "fixture.gguf"
    weight.write_bytes(b"GGUF" + b"x" * 128)
    docs_root = tmp_path / "docs"
    docs_root.mkdir()
    for name in ("04-买家安装与使用说明.md", "05-第三方许可清单.md"):
        (docs_root / name).write_text("fixture", encoding="utf-8")
    doc = docs_root / "guide.md"
    doc.write_text("before", encoding="utf-8")
    monkeypatch.setattr(delivery, "ROOT", docs_root)
    return {
        "zip_path": tmp_path / "release.zip",
        "source": source,
        "files": [exe, weight],
        "apk": None,
        "docs": [doc],
        "round_name": "r1",
        "manifest": "test",
        "weights": [weight],
        "notice": "test",
    }


def check_package(delivery: ModuleType, tree: dict[str, Any]) -> list[str]:
    return list(
        delivery.read_back(
            tree["zip_path"],
            tree["source"],
            tree["files"],
            tree["source"] / "小夜.exe",
            tree["weights"],
            tree["docs"],
        )
    )


def test_archive_is_atomic_and_weights_are_not_recompressed(
    delivery: ModuleType, package_tree: dict[str, Any]
) -> None:
    delivery.build_package(**package_tree)
    assert check_package(delivery, package_tree) == []
    assert not package_tree["zip_path"].with_name("release.zip.partial").exists()
    with zipfile.ZipFile(package_tree["zip_path"]) as archive:
        assert archive.getinfo("小夜/模型/fixture.gguf").compress_type == zipfile.ZIP_STORED
        assert ".partial" not in archive.read("先看我一页.txt").decode("utf-8")


def test_failed_packaging_preserves_the_previous_file(
    delivery: ModuleType, package_tree: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    package_tree["zip_path"].write_bytes(b"previous release")

    def fail(*args: Any) -> None:
        raise OSError("disk write failed")

    monkeypatch.setattr(delivery, "add_file", fail)
    with pytest.raises(OSError):
        delivery.build_package(**package_tree)
    assert package_tree["zip_path"].read_bytes() == b"previous release"


def test_readback_detects_same_size_exe_or_document_changes(
    delivery: ModuleType, package_tree: dict[str, Any]
) -> None:
    delivery.build_package(**package_tree)
    exe = package_tree["source"] / "小夜.exe"
    exe.write_bytes(b"x" * exe.stat().st_size)
    package_tree["docs"][0].write_text("after!", encoding="utf-8")
    bad = check_package(delivery, package_tree)
    assert any("SHA256" in line for line in bad)
    assert any("文档内容" in line for line in bad)


def test_readback_refuses_archive_traversal(
    delivery: ModuleType, package_tree: dict[str, Any]
) -> None:
    delivery.build_package(**package_tree)
    with zipfile.ZipFile(package_tree["zip_path"], "a") as archive:
        archive.writestr("../outside.txt", "not extracted")
    assert any("越界路径" in line for line in check_package(delivery, package_tree))


def test_cleanup_requires_full_verification_and_preserves_user_data(tmp_path: Path) -> None:
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if powershell is None or sys.platform != "win32":
        pytest.skip("Windows release cleanup test")
    root = tmp_path / "JarvisBuild"
    out = tmp_path / "delivery"
    out.mkdir()
    for name in ("r1", "r2", "r3"):
        exe = root / name / "dist/小夜/小夜.exe"
        exe.parent.mkdir(parents=True)
        exe.write_bytes(b"exe")
    private = root / "r2/dist/小夜/数据"
    private.mkdir()
    (private / "keep.db").write_bytes(b"private fixture")
    for name in ("r1", "r2"):
        apk = root / "mobile" / name / "小夜.apk"
        apk.parent.mkdir(parents=True)
        apk.write_bytes(b"apk")
    archive = out / "小夜-电脑+手机-r3.zip"
    archive.write_bytes(b"archive fixture")
    proof: dict[str, Any] = {
        "ok": True,
        "full_crc": False,
        "round": "r3",
        "zip": str(archive),
        "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        "source": str(root / "r3/dist/小夜"),
        "exe_sha256": hashlib.sha256(b"exe").hexdigest(),
        "apk": str(root / "mobile/r2/小夜.apk"),
        "apk_sha256": hashlib.sha256(b"apk").hexdigest(),
    }
    evidence = tmp_path / "proof.json"
    evidence.write_text(json.dumps(proof), encoding="utf-8")
    args = [
        powershell,
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(ROOT / "scripts/cleanup_releases.ps1"),
        "-Root",
        str(root),
        "-Out",
        str(out),
        "-KeepRound",
        "r3",
        "-KeepMobileRound",
        "r2",
        "-Verification",
        str(evidence),
        "-Apply",
    ]
    rejected = subprocess.run(args, capture_output=True, timeout=60, check=False)
    assert rejected.returncode != 0
    assert (root / "r1").exists()
    proof["full_crc"] = True
    evidence.write_text(json.dumps(proof), encoding="utf-8")
    result = subprocess.run(args, capture_output=True, timeout=60, check=False)
    assert result.returncode == 0, result.stdout.decode(errors="replace") + result.stderr.decode(
        errors="replace"
    )
    assert not (root / "r1").exists()
    assert (private / "keep.db").exists()
    assert (root / "r3/dist/小夜/小夜.exe").exists()
    assert not (root / "mobile/r1").exists()
    assert (root / "mobile/r2/小夜.apk").exists()


def test_buyer_commands_follow_the_release_but_history_is_preserved(
    tmp_path: Path, delivery: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(delivery, "ROOT", tmp_path)
    guide = tmp_path / "04-买家安装与使用说明.md"
    guide.write_text(
        "certutil -hashfile 小夜-电脑+手机-r86.zip SHA256\n历史 r77 速度 3 tok/s\n",
        encoding="utf-8",
    )
    delivery.sync_buyer_commands("r87")
    text = guide.read_text(encoding="utf-8")
    assert "小夜-电脑+手机-r87.zip" in text
    assert "r86.zip" not in text
    assert "历史 r77 速度 3 tok/s" in text


#: Directories whose contents are third-party or generated; their files are not ours to fix.
_BATCH_SKIP = (".venv", "node_modules", ".git", "vendor", "build", "dist", "__pycache__")


def test_every_batch_file_we_ship_uses_crlf() -> None:
    """cmd.exe reads batch files byte-by-byte, so a bare LF breaks the whole file.

    The observed failure was ``一键做交付包.bat`` printing 「'♦改快捷方式清旧包。' is not
    recognized as an internal or external command」 -- the multi-line ``if ( ... )`` block
    was cut apart, its ``echo`` lines ran as commands, and UTF-8 got read mid-character.
    Python tools keep rewriting these files with ``\n``; this is the tripwire.
    """
    offenders: list[str] = []
    for path in sorted((ROOT / "scripts").rglob("*.bat")) + sorted(ROOT.glob("*.bat")):
        if any(part in _BATCH_SKIP for part in path.parts):
            continue
        raw = path.read_bytes()
        bare = raw.count(b"\n") - raw.count(b"\r\n")
        if bare:
            offenders.append(f"{path.relative_to(ROOT)}：{bare} 个裸 LF")
    assert not offenders, "批处理必须全 CRLF：" + "、".join(offenders)


def test_the_delivery_bat_copies_stay_identical() -> None:
    """One lives in ``scripts/templates`` (tracked), one in ``闲鱼展示内容`` (the seller's copy).

    They are the same tool handed to the same person twice; a fix applied to only one of
    them is how the seller keeps running the broken one.
    """
    tracked = ROOT / "scripts/templates/一键做交付包.bat"
    seller = ROOT / "闲鱼展示内容/一键做交付包.bat"
    assert tracked.read_bytes() == seller.read_bytes(), "两份一键做交付包.bat 已经不一样了"


def test_the_round_is_read_out_of_the_paths_the_builder_was_given() -> None:
    """`check_update` 比的只有 rNN，而打包时唯一知道这个数的地方是 --distpath/--workpath。

    这条不是装饰：spec 里那段识别搬进这个文件才测得到，而它一旦认歪，发出去的每个 exe
    都"读不出自己是第几轮"，买家那句「我这版旧不旧」就永远没有答案。
    """
    assert round_from(r"E:\BianChengGongJu\JarvisBuild\r107\dist", "") == "r107"
    assert round_from("E:/Build/r9/dist/小夜") == "r9"
    assert round_from("", r"E:\BianChengGongJu\JarvisBuild\r107\build") == "r107"
    # 两个都在、各自还带一段（手工跑过一次 PyInstaller 忘了改 --workpath）：以 --distpath 为准
    assert round_from(r"E:\B\r107\build", r"E:\B\r108\dist") == "r108"
    # 同一条路径里两段都像轮次：认**最右那一段**，因为那才是真正装产物的一层
    assert round_from(r"E:\B\r100\副本\r107\dist") == "r107"


def test_an_unlabelled_build_hint_is_left_blank_instead_of_guessed() -> None:
    """认不出来就回空串。回 0 等于「任何真实轮次都比它新」= 所有人永远看到「有新版」。

    顺带说清一件事：路径里任何一个 `rNNN` 那样的目录段都会被当轮次收下（包括碰巧叫
    `r2026` 的）。没打算在这儿防它 —— 发版时 `deliver_release.ps1` 会拿打包出来的
    自检报告比 `probe.build.round` 和 `-Round` 那一个数，认错了当场就不发货。
    """
    assert round_from(r"E:\BianChengGongJu\JarvisBuild\dist") == ""
    assert round_from(None, "") == ""
    assert round_from(r"C:\temp\release\2026\dist") == ""
    assert round_from(r"C:\temp\r2026\dist") == "r2026"


def test_build_info_stamps_the_round_and_refuses_a_thing_that_is_not_one(
    tmp_path: Path,
) -> None:
    assert build_info(tmp_path, "r107")["round"] == "r107"
    assert build_info(tmp_path, "r107")["schema"] == 2
    # 没标签的构建（直接跑一次 PyInstaller）不许编一个轮次出来
    assert build_info(tmp_path)["round"] == ""
    with pytest.raises(ValueError):
        build_info(tmp_path, "0.1.0")
