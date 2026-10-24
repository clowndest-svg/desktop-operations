# ruff: noqa: E501
# Long localized console/document lines are preserved for copy-paste; logic rules remain enforced.
"""做一个能发给买家的交付包：只装 dist/小夜，并且**挡住隐私**和**挡住过期**的那两步。

为什么要脚本而不是右键压缩
    打包版默认把数据根建在 exe 旁边的 ``数据\\`` 目录，所以只要你自己双击过一次那个 dist 里的
    小夜.exe，里面就可能有你的对话数据库、长期记忆、审计日志、录过的音。"整个目录压一下"
    就会把这些一起发出去。本脚本发现任何一项就直接失败，不给你"顺手压了"的机会。

包里的布局（2026-10-07 起，**解压一层就到位**）
    ``先看我一页.txt`` + ``小夜/``（程序本体，双击里面的 ``小夜.exe``）+ ``手机/`` + ``文档/`` + ``清单.txt``。
    以前是"合并包再套一层电脑 zip"：买家要解压两遍、白占 5 GB 磁盘、多花三分钟把已经压好的
    5 GB 再压一次（实测省不到 3 MB）。

用法::

    .venv\\Scripts\\python.exe 闲鱼展示内容\\制作交付包.py --round r76
    .venv\\Scripts\\python.exe 闲鱼展示内容\\制作交付包.py --round auto --with-mobile --with-docs
    .venv\\Scripts\\python.exe 闲鱼展示内容\\制作交付包.py --check 闲鱼展示内容\\交付包\\小夜-电脑+手机-r76.zip
    .venv\\Scripts\\python.exe 闲鱼展示内容\\制作交付包.py --dry-run

``--round auto`` 自己挑最新的、真有 ``dist/小夜/小夜.exe`` 的那一轮。
``--with-mobile`` 带上最新手机 APK，``--with-docs`` 把四份买家说明拷进 ``交付包/文档/`` 并装进包里，
``--pc-zip`` 另出一份只含电脑包的 ``小夜-rNN.zip``（多花 8 分钟，只有你确实要分开两个链接时才加）。
产物落在 ``闲鱼展示内容/交付包/``，另写 ``清单-<轮次>.txt``（每件产物的 sha256、随包权重的 md5、
git HEAD 与脏标记、隐私检查结论）和 ``发货记录.txt``。双击 ``一键做交付包.bat`` 等于第二条命令。

跑完之后脚本还会替你做两件"以前靠记性"的事：把 ``03-怎么发给买家.md`` 和 ``README.md`` 里包在
``<!-- 交付包数字 -->`` 之间的表**改写成本次的实测值**（数字手抄一定会烂，r65 那张表就是这么留在
包外的），以及扫一遍对外文案里还写着**别的轮次**的带数字句子，点名到哪一行让你去看（只报不改）。
"""

from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import struct
import subprocess
import sys
import time
import zipfile
from pathlib import Path

from release_manifest import (
    digest_file,
    read_json,
    scoped_path,
    source_fingerprint,
    valid_round,
    write_json,
)

ROOT = Path(__file__).resolve().parents[1] / "闲鱼展示内容"
BUILD_ROOT = Path("E:/DaiMa/XiaoYeBuild")
REPO_ROOT = ROOT.parent

# 双击运行时当前目录是 闲鱼展示内容/，jarvis 不在 sys.path 上 —— 从仓库根补进去，
# 否则这脚本只能在仓库根跑，而它存在的意义就是让人不用记命令。
sys.path.insert(0, str(REPO_ROOT))

# 打包版没设 JARVIS_HOME 时数据根的名字、随包权重所在的目录名，都从建它们的那段代码里读，
# 不在这儿抄一份 —— 否则改了代码这份检查会安静地失效。
from jarvis.config.paths import BUNDLE_DATA_DIRNAME  # noqa: E402
from jarvis.llm.local_server import MODEL_DIRNAME  # noqa: E402

# exe 旁边那一层出现这些名字，说明这台机器上真跑过它。
FORBIDDEN_FILES = {"config.yaml", "preferences.json", ".env", "credentials.dpapi.json"}
FORBIDDEN_SUFFIXES = {".db", ".db-wal", ".db-shm", ".jsonl", ".wav", ".mp3", ".key"}

# 随合并包发给买家的说明。01 是给你自己粘闲鱼用的，03 是"怎么发"的内部流程，都不进包。
BUYER_DOCS = (
    "02-项目介绍.md",
    "04-买家安装与使用说明.md",
    "05-第三方许可清单.md",
    "06-接本地大模型.md",
    "07-新版界面与功能入口.md",
    "小夜logo.webp",
)

# 包里必须有这几条成员 —— 少一条就是"买家拿到的不是完整交付"，宁可失败别静默。
PC_DIR = "小夜"
REQUIRED_MEMBERS = (
    "先看我一页.txt",
    f"{PC_DIR}/小夜.exe",
    f"{PC_DIR}/授权.txt",
    f"{PC_DIR}/第三方许可清单.txt",
    f"{PC_DIR}/买家安装与使用说明.md",
    f"{PC_DIR}/{MODEL_DIRNAME}/许可与来源.txt",
    "清单.txt",
)
# 除了 REQUIRED_MEMBERS，电脑那一半还多这一条（清单.txt 在根上，不算进 小夜/ 的账）
INJECTED_UNDER_PC = (
    "授权.txt",
    "第三方许可清单.txt",
    "买家安装与使用说明.md",
    f"{MODEL_DIRNAME}/许可与来源.txt",
)

# 数字被自动改写的那一段，两个文件里都用这对标记框住。
BLOCK_START = "<!-- 交付包数字：开始"
BLOCK_END = "<!-- 交付包数字：结束 -->"

WEB_ASSETS = Path("_internal") / "jarvis" / "ui" / "web" / "assets"
DISK_HEADROOM_MB = 2048


def now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def human(size: int) -> str:
    return f"{size / 1024 / 1024:.1f} MB"


def digest_of(path: Path, algo: str, chunk: int = 1 << 20) -> str:
    hash_ = hashlib.new(algo)
    with path.open("rb") as handle:
        while block := handle.read(chunk):
            hash_.update(block)
    return hash_.hexdigest()


def sha256_of(path: Path) -> str:
    return digest_of(path, "sha256")


def md5_of(path: Path) -> str:
    return digest_of(path, "md5")


def scan(source: Path) -> list[str]:
    """Anything in this tree that says "somebody already ran this and left their life in it".

    Deliberately narrow. The first version walked the whole bundle and flagged 91 things,
    of which the useful count was zero: ``funasr`` ships ``__pycache__`` everywhere,
    ``hydra`` ships test fixtures literally named ``config.yaml``, and ``certifi`` ships
    ``cacert.pem``. A check that cries wolf gets bypassed by hand within a minute -- and
    hand-packing the folder is exactly the thing that leaks a chat database to a stranger.
    So: the data root is refused wholesale if it exists at all, and only the level right
    next to the exe is matched by name.
    """
    hits: list[str] = []
    for path in source.rglob("*"):
        if path.is_symlink() or path.is_junction():
            hits.append(f"不打包链接/联接：{path.relative_to(source)}")
    for data in (source / BUNDLE_DATA_DIRNAME, source / "用户数据"):
        if data.is_dir():
            count = sum(1 for entry in data.rglob("*") if entry.is_file())
            hits.append(f"数据根 {data.name}/ 存在，里面有 {count} 个文件（这台机器上运行过它）")
    for entry in sorted(source.iterdir()):
        if entry.is_file() and (
            entry.name in FORBIDDEN_FILES or entry.suffix in FORBIDDEN_SUFFIXES
        ):
            hits.append(f"exe 旁边有 {entry.name}")
    return hits


def newest_round(build_root: Path) -> str:
    """The newest rNN that actually holds a built exe; anything else is a half build."""
    candidates = [
        d
        for d in build_root.iterdir()
        if d.is_dir()
        and re.fullmatch(r"r\d+", d.name)
        and (d / "dist" / "小夜" / "小夜.exe").is_file()
    ]
    if not candidates:
        return ""
    return max(candidates, key=lambda d: int(d.name[1:])).name


def newest_apk(build_root: Path) -> Path | None:
    apks = [
        p
        for p in (build_root / "mobile").glob("r*/小夜-*.apk")
        if re.fullmatch(r"r[0-9]+", p.parent.name)
    ]
    return max(apks, key=lambda p: (int(p.parent.name[1:]), p.name)) if apks else None


def git_state() -> tuple[str, bool]:
    """HEAD sha and whether the tree is dirty, so a package can say what it came from."""
    try:
        head = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        return head, bool(status)
    except (OSError, subprocess.CalledProcessError):
        return "unknown", True


def mobile_source_dirty() -> list[str]:
    """Uncommitted edits under the mobile half: the APK may not match what is on disk."""
    try:
        status = subprocess.run(
            ["git", "status", "--porcelain", "--", "frontend/src/mobile", "frontend/android"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return ["读不到 git 状态，无法判断手机源码是否干净"]
    return [line for line in status.splitlines() if line.strip()]


def bundled_weights(source: Path) -> list[Path]:
    """随包大模型：exe 旁边那个 ``模型`` 目录里的 .gguf（路径口径和 local_server 一致）。

    大的排前面 —— 出厂"自动"选中的就是最大的那一个，第一条检查要盯的是它。
    """
    return sorted((source / MODEL_DIRNAME).glob("*.gguf"), key=lambda path: -path.stat().st_size)


def freshness_warnings(source: Path, exe: Path) -> list[str]:
    """打包之后又改过的东西，不会出现在这一版包里 —— 这条 r75 真踩过，屏幕上拍到的就是旧包。"""
    notes: list[str] = []
    metadata = source / "_internal" / "jarvis" / "buildinfo.json"
    try:
        info = read_json(metadata)
        if info.get("source_sha256") != source_fingerprint(REPO_ROOT):
            notes.append("PC 包的源码内容指纹与当前工作区不一致；必须重新构建")
        if info.get("edition") == "buyer":
            for relative in ("运行库/WebView2/msedgewebview2.exe",):
                if not (source / relative).is_file():
                    notes.append(f"买家包缺少离线必备资源：{relative}")
    except (OSError, ValueError):
        notes.append("PC 包缺少可核对的源码指纹，不能作为本轮交付")
    built = source / WEB_ASSETS
    repo = REPO_ROOT / "jarvis" / "ui" / "web" / "assets"
    if built.is_dir() and repo.is_dir():
        here = {p.name: p.stat().st_size for p in built.iterdir() if p.is_file()}
        mine = {p.name: p.stat().st_size for p in repo.iterdir() if p.is_file()}
        if here != mine:
            notes.append(
                f"包里的前端资源（{sorted(here)}）和仓库现在这份（{sorted(mine)}）对不上 —— "
                f"改完前端要重新打包，否则买家看到的就是旧界面"
            )
    else:
        notes.append(f"读不到 {built} 或 {repo}，前端新鲜度这条**没验成**")

    stamp = exe.stat().st_mtime
    newest: tuple[float, Path] | None = None
    for pattern in (
        "jarvis/**/*.py",
        "frontend/src/**/*",
        "frontend/index.html",
        "frontend/vite.config.ts",
    ):
        for path in REPO_ROOT.glob(pattern):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            got = path.stat().st_mtime
            if got > stamp and (newest is None or got > newest[0]):
                newest = (got, path)
    if newest is not None:
        notes.append(
            f"exe 构建于 {time.strftime('%m-%d %H:%M', time.localtime(stamp))}，之后源码还改过 —— 最新是 "
            f"{newest[1].relative_to(REPO_ROOT)}（{time.strftime('%m-%d %H:%M', time.localtime(newest[0]))}）。"
            f"这一版包里**不含**这些改动；要带就重新打包，不带就当它是下一轮"
        )
    return notes


_FIXED_KIND = {
    0: "<B",
    1: "<b",
    2: "<H",
    3: "<h",
    4: "<I",
    5: "<i",
    6: "<f",
    7: "<?",
    10: "<Q",
    11: "<q",
    12: "<d",
}


def gguf_meta(path: Path) -> dict[str, str]:
    """权重文件自己头部写了什么就说什么，只取 general.* 里的字符串项。

    GGUF 的类型编号里 8 是字符串、9 是数组 —— 把 8 当 uint64 读会一路读到天上去；
    数组必须整个读完再丢，不然下一个键的偏移就错了（这两条都是当场踩出来的）。
    """
    out: dict[str, str] = {}
    try:
        with open(path, "rb") as fh:
            if fh.read(4) != b"GGUF":
                return {}
            version = struct.unpack("<I", fh.read(4))[0]
            wide = "<Q" if version >= 3 else "<I"
            span = 8 if version >= 3 else 4
            fh.read(span)  # 张量数：用不上，但偏移要走过
            pairs = struct.unpack(wide, fh.read(span))[0]

            def read(kind: int, keep: bool):
                if kind in _FIXED_KIND:
                    fmt = _FIXED_KIND[kind]
                    raw = fh.read(struct.calcsize(fmt))
                    return struct.unpack(fmt, raw)[0] if keep else None
                if kind == 8:
                    size = struct.unpack("<Q", fh.read(8))[0]
                    raw = fh.read(size)
                    return raw.decode("utf-8", "replace") if keep else None
                if kind == 9:
                    elem = struct.unpack("<I", fh.read(4))[0]
                    count = struct.unpack("<Q", fh.read(8))[0]
                    head: list[object] = []
                    for index in range(count):
                        got = read(elem, True)
                        if index < 3:
                            head.append(got)
                    return head if keep else None
                raise ValueError(f"GGUF 值类型 {kind}")

            for _ in range(pairs):
                key = read(8, True)
                kind = struct.unpack("<I", fh.read(4))[0]
                keep = isinstance(key, str) and key.startswith("general.") and kind != 9
                got = read(kind, bool(keep))
                if keep and isinstance(got, (str, int, float, bool)):
                    out[str(key)] = str(got)
    except (OSError, EOFError, struct.error, ValueError):
        return {}
    return out


def evidence(meta: dict[str, str]) -> str:
    """许可那一行：文件头写了就照抄，没写就明说依据不是文件自报。"""
    head = meta.get("general.license") or ""
    if head:
        link = meta.get("general.license.link") or ""
        return f"许可：{head}（依据：这个文件自己的 GGUF 头部写了 general.license）" + (
            f"，链接 {link}" if link else ""
        )
    return (
        "许可：Apache License 2.0（全文附在本页末尾）。依据：**这个文件头部没有 general.license 字段**，"
        "所以依据不是文件自报，而是构建机上从上游仓库取回的 LICENSE 文本（同一份 Apache-2.0 标准文本）"
    )


def weight_problems(weights: list[Path]) -> list[str]:
    """装箱前的验真：开头不是 GGUF 魔数、或者小得可疑的一律拦下来。"""
    notes: list[str] = []
    for item in weights:
        size = item.stat().st_size
        with open(item, "rb") as fh:
            magic = fh.read(4)
        if magic != b"GGUF":
            notes.append(
                f"{item.name}：开头不是 GGUF 魔数（读到 {magic!r}），{size:,} 字节 —— 像占位/空文件。"
                "这种文件打进包，买家在「选哪个离线模型」里看得见它，点下去必定加载失败"
            )
        elif size < 1_000_000:
            notes.append(f"{item.name}：只有 {size:,} 字节，不像一份权重")
    return notes


def over_4g(weights: list[Path]) -> list[tuple[str, int]]:
    """哪些成员超过 4 GiB —— 资源管理器按 32 位显示，那几行的数字必然是错的。"""
    return [(item.name, item.stat().st_size) for item in weights if item.stat().st_size >= 2**32]


def apache_text() -> str:
    """Apache-2.0 全文只从本机 wheel 里现成的标准文本取，绝不自己"背"一遍。"""
    site_packages = REPO_ROOT / ".venv" / "Lib" / "site-packages"
    found = sorted(site_packages.glob("*/licenses/LICENSE.APACHE")) or sorted(
        site_packages.rglob("LICENSE.APACHE")
    )
    for candidate in found:
        text = candidate.read_text(encoding="utf-8", errors="replace").strip()
        head = text[:200]
        if "Apache License" in head and "Version 2.0" in head:
            return text + "\n"
    return ""


def model_notice(weights: list[Path], round_name: str, weight_md5: dict[str, str]) -> str:
    """随包权重的许可与来源一页 —— 发出去的东西，来源要能在包里查到，别只在文案里说。"""
    lines = [
        f"随包大模型 · 许可与来源（构建 {round_name}）",
        "",
        "这一页回答的是「这东西在你包里、许可是什么、从哪来」，逐条都能自己在文件里核对：",
        "",
    ]
    for item in weights:
        meta = gguf_meta(item)
        lines += [
            f"文件：{MODEL_DIRNAME}\\{item.name}",
            f"大小：{item.stat().st_size:,} 字节（{human(item.stat().st_size)}）",
            f"MD5：{weight_md5.get(item.name) or md5_of(item)}   ← 下载断了先单独校这一个文件",
            f"文件头自报：general.name = {meta.get('general.name') or '（没写）'}；"
            f"general.architecture = {meta.get('general.architecture') or '（没写）'}",
            evidence(meta),
            "",
        ]
    lines += [
        "这四份都是阿里通义 Qwen 团队发布的开源权重（本包内是量化版：Q4_K_M / Q8_0）。",
        "每份的许可依据写在它自己那一段：头部有 general.license 的照抄，没写的明说依据不是文件自报。",
        "本软件（小夜）不声称这些模型是自己训练的；模型与其输出按各自 Apache-2.0 条款使用。",
        "想自己核对：任选一份看开头四个字节是不是 GGUF、general.license 写没写，再比对本页的 MD5。",
        "",
        "运行模型的代码是 llama.cpp（MIT），随包在 _internal\\jarvis\\vendor\\llama\\。",
        "  llama.cpp 的 MIT 全文与 LLVM OpenMP 许可随包位于 _internal/licenses/。",
        "",
        "投屏用的代码是 scrcpy（Apache-2.0，上游 github.com/Genymobile/scrcpy），随包在",
        "  _internal\\jarvis\\vendor\\scrcpy\\，那 16 个文件里带着一份 LICENSE.txt（Apache-2.0 全文，11,387 字节）。",
        "  捆绑依赖的许可和出处见 _internal/licenses/README.md 与同目录全文：",
        "     SDL 3.4.18（zlib，libsdl.org）、FFmpeg：avcodec-63.1.102 / avformat-63.1.102 /",
        "     avutil-61.1.102 / swresample-7（LGPL-2.1+，ffmpeg.org）、libusb 1.0.30（LGPL-2.1，libusb.info）、",
        "     adb 1.0.41 / 37.0.1-15733141 + AdbWinApi.dll（Android platform-tools，Apache-2.0，developer.android.com）。",
        "     版本号取自 scrcpy.exe --version 在本机实测打印的那几行，不是抄上游 README。",
        "     LGPL 那两类的义务是给到源码出处（上游版本号 + 下载地址），对外要写清不是我们写的，",
        "     也别声称这些是本项目的一部分。",
        "  投屏功能在本机没有连过任何真手机：代码侧的状态机有测试，真机那一段未验收。",
        "",
    ]
    text = apache_text()
    if text:
        lines += [
            "附：Apache License 2.0 全文（标准文本，取自本机依赖的 LICENSE.APACHE，未改写）",
            "",
            text,
        ]
    else:
        lines += [
            "附：Apache License 2.0 全文本机没找到现成标准文本，全文见 https://www.apache.org/licenses/LICENSE-2.0"
        ]
    return "\n".join(lines) + "\n"


def buyer_first_page(round_name: str, apk: Path | None, weights: list[Path], zip_name: str) -> str:
    """zip 根上那张"先看我一页"：怕买家根本不翻 文档\\。"""
    weight_line = (
        "模型文件（在「模型」目录里，解压完逐个右键比大小）："
        + "；".join(f"{item.name} 应该是 {item.stat().st_size:,} 字节" for item in weights)
        + "。"
        if weights
        else ""
    )
    zip4g = over_4g(weights)
    display_note = (
        "   显示上的坑：在压缩包里 "
        + "、".join(f"{name} 被写成 {size % 2**32:,} 字节（真实 {size:,}）" for name, size in zip4g)
        + "（超过 4 GB 那一格资源管理器不显示对）。那不代表包坏了 —— 要看得准就解压完在硬盘上右键属性。"
        if zip4g
        else ""
    )
    return "\n".join(
        [
            f"小夜 · 桌面 AI 语音助手 · 构建 {round_name}",
            "",
            "三步：",
            "1) 把整个压缩包完整解压到可写的非系统盘。解压一层应直接看到 小夜\\ 文件夹，",
            "   里面有小夜.exe 和 模型\\ 目录。",
            "   Windows 自带的右键「全部解压」也可以用（实测解出来的模型大小和 md5 都跟包里一致）。",
            display_note,
            "2) 双击 小夜\\小夜.exe。未签名程序可能出现安全提示，请核对来源及校验信息。",
            "   别把 小夜.exe 单独拷出来运行；旁边必须有 _internal、模型及随包运行库。",
            f"3) 设置里自带模型显示可用后即可打字提问；包里带了 {len(weights)} 个离线大模型，"
            "   无需 API Key；买家版自动按空闲内存选模型，也可在设置里调整。",
            f"   {weight_line}小了或者没这个文件 = 解压没解全，重来一遍（自带解压和 7-Zip 都行）。",
            "",
            "买家版数据在 小夜\\用户数据\\（旧版为 数据\\），设置中可查看实际目录。升级请保留用户数据。",
            "本地模型无需Key即可聊天、调用允许的工具与免Key联网检索。云端服务可选，需买家自己的账号。",
            "详细的（语音、宠物、告警、手机遥控、边界说明）在 文档\\04-买家安装与使用说明.md，那份才是完整的。",
            "",
            f"校验你拿到的是原包：certutil -hashfile {zip_name} SHA256   （值在压缩包外的清单与发货记录里）",
            "本份授权：一台机器自用；不得转售、不得二次分发成品或源码。第三方许可见 小夜\\第三方许可清单.txt。",
            "发这份包的人：有问题带上「哪一步 + 屏幕上的红字原文」来问，比一句「打不开」快十倍。",
        ]
        + (
            [
                "",
                f"手机遥控：安卓 APK 在 手机\\{apk.name}（Beta，没在真机装过；浏览器那条路是保底）。",
            ]
            if apk
            else []
        )
        + [""]
    )


def add_file(archive: zipfile.ZipFile, path: Path, arcname: str, stats: list[int]) -> None:
    if path.suffix.lower() == ".gguf":
        archive.write(path, arcname, zipfile.ZIP_STORED)
    else:
        archive.write(path, arcname, zipfile.ZIP_DEFLATED, compresslevel=6)
    stats[0] += path.stat().st_size
    if stats[0] - stats[1] >= (512 << 20):
        stats[1] = stats[0]
        print(f"  … 已写入 {human(stats[0])}（{time.strftime('%H:%M:%S')}）", flush=True)


def build_package(
    zip_path: Path,
    source: Path,
    files: list[Path],
    apk: Path | None,
    docs: list[Path],
    round_name: str,
    manifest: str,
    weights: list[Path],
    notice: str,
) -> None:
    """一次成型：小夜/ + 手机/ + 文档/ + 先看我一页 + 清单，全在同一个归档里。"""
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    stats = [0, 0]
    final_path = zip_path
    zip_path = final_path.with_name(final_path.name + ".partial")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        archive.writestr(
            "先看我一页.txt", buyer_first_page(round_name, apk, weights, final_path.name)
        )
        for item in files:
            add_file(archive, item, f"小夜/{item.relative_to(source)}", stats)
        notes = ROOT / "04-买家安装与使用说明.md"
        archive.write(notes, "小夜/买家安装与使用说明.md")
        archive.writestr(
            "小夜/授权.txt",
            (
                f"小夜 桌面 AI 语音助手 · 构建 {round_name}\n"
                "本份授权：一台机器自用；不得转售、不得二次分发成品或源码。\n"
                "第三方库按其各自许可随包分发，声明见 第三方许可清单.txt；"
                "随包大模型的许可与来源见 模型/许可与来源.txt。\n"
            ).encode(),
        )
        license_notes = ROOT / "05-第三方许可清单.md"
        archive.write(license_notes, "小夜/第三方许可清单.txt")
        archive.writestr(f"小夜/{MODEL_DIRNAME}/许可与来源.txt", notice)
        if apk is not None:
            add_file(archive, apk, f"手机/{apk.name}", stats)
        for doc in docs:
            archive.write(doc, f"文档/{doc.name}", zipfile.ZIP_DEFLATED)
        archive.writestr("清单.txt", manifest)
    bad = read_back(zip_path, source, files, source / "小夜.exe", weights, docs)
    if bad:
        raise RuntimeError("临时交付包校验失败，旧文件未替换：" + "; ".join(bad))
    zip_path.replace(final_path)


def read_back(
    zip_path: Path,
    source: Path,
    files: list[Path],
    exe: Path,
    weights: list[Path],
    docs: list[Path],
) -> list[str]:
    """写完必须回读 —— 这一步是"我说我打好了"和"买家真能解开"之间的差。"""
    bad: list[str] = []
    with zipfile.ZipFile(zip_path) as archive:
        crc = archive.testzip()
        if crc is not None:
            bad.append(f"CRC 校验第一个坏掉的成员：{crc}")
        members = archive.infolist()
        infos = {i.filename: i for i in members}
        if len(infos) != len(members):
            bad.append("包内出现重复文件名")
        for name in infos:
            parts = name.replace("\\", "/").split("/")
            if name.startswith(("/", "\\")) or ".." in parts or ":" in parts[0]:
                bad.append(f"包内出现越界路径：{name}")
        missing = [name for name in REQUIRED_MEMBERS if name not in infos]
        if missing:
            bad.append("缺成员：" + "、".join(missing))
            return bad
        exe_name = f"{PC_DIR}/{exe.name}"
        if infos[exe_name].file_size != exe.stat().st_size:
            bad.append(
                f"exe 大小对不上：包里 {infos[exe_name].file_size} vs 源 {exe.stat().st_size}"
            )
        with archive.open(exe_name) as handle:
            if hashlib.file_digest(handle, "sha256").hexdigest() != digest_file(exe):
                bad.append("exe 内容 SHA256 对不上")
        for doc in docs:
            member = "文档/" + doc.name
            if member not in infos:
                bad.append(f"缺少买家文档：{doc.name}")
            else:
                with archive.open(member) as handle:
                    if hashlib.file_digest(handle, "sha256").hexdigest() != digest_file(doc):
                        bad.append(f"买家文档内容已变：{doc.name}")
        under_pc = [n for n in infos if n.startswith(f"{PC_DIR}/") and not n.endswith("/")]
        want = len(files) + len(INJECTED_UNDER_PC)
        if len(under_pc) != want:
            bad.append(
                "小夜/ 下面应该是 "
                f"{want}"
                " 条（源文件 "
                f"{len(files)}"
                " + 注入文本 "
                f"{len(INJECTED_UNDER_PC)}"
                "），实际 "
                f"{len(under_pc)}"
            )
        for item in weights:
            name = f"{PC_DIR}/{MODEL_DIRNAME}/{item.name}"
            if name not in infos:
                bad.append(f"随包权重没进包：{name}")
            elif infos[name].file_size != item.stat().st_size:
                bad.append(
                    f"{name} 大小对不上：包里 {infos[name].file_size} vs 源 {item.stat().st_size}"
                )
        leaked = [
            name
            for name in infos
            if f"{PC_DIR}/{BUNDLE_DATA_DIRNAME}/" in name
            or f"{PC_DIR}/用户数据/" in name
            or (
                Path(name).name in FORBIDDEN_FILES
                and not (
                    Path(name).name == "config.yaml"
                    and name.startswith(f"{PC_DIR}/_internal/hydra/test_utils/configs/")
                )
            )
            or (Path(name).suffix in FORBIDDEN_SUFFIXES and not _scipy_test_audio(name))
        ]
        if leaked:
            bad.append(f"包里出现隐私形状的文件（{len(leaked)} 条）：" + "、".join(leaked[:8]))
        print(
            f"  条目数 {len(infos)}：源 {len(files)} 个文件 + 注入文本"
            # 这一行数的就是买家文档那几份，以前写成「手机/文档」，不带手机时也会印出来，
            # 看着像包里真有手机目录（r122 被它绊过一次）。
            + (f" + 买家文档（{len(docs)} 份）" if docs else "")
        )
    return bad


def _scipy_test_audio(name: str) -> bool:
    """Only synthetic WAV fixtures shipped inside the portable SciPy dependency.

    Do not exempt the engine's input/output/user folders or all site-packages.
    """
    prefix = f"{PC_DIR}/本地引擎/venv/Lib/site-packages/scipy/io/tests/data/"
    return name.startswith(prefix) and bool(
        re.fullmatch(r"test-\d+Hz[-A-Za-z0-9.]*\.wav", name[len(prefix) :])
    )


def stamp_docs(facts03: str, facts_readme: str) -> list[str]:
    """把标记块换成这次跑出来的实测数字；标记没找到就报，绝不往文件里瞎插。"""
    notes: list[str] = []
    for name, block in (("03-怎么发给买家.md", facts03), ("README.md", facts_readme)):
        path = ROOT / name
        if not path.is_file():
            notes.append(f"{name} 不存在，跳过")
            continue
        text = path.read_text(encoding="utf-8")
        start = text.find(BLOCK_START)
        end = text.find(BLOCK_END)
        if start < 0 or end < 0 or end < start:
            notes.append(f"{name} 里找不到「{BLOCK_START}」那对标记，数字得手改")
            continue
        line_end = text.index("\n", start) + 1
        rebuilt = text[:line_end] + block.rstrip("\n") + "\n" + text[end:]
        if rebuilt != text:
            path.write_text(rebuilt, encoding="utf-8")
            notes.append(f"{name}：数字表已改写成本次实测")
        else:
            notes.append(f"{name}：数字表本来就是这次的值，没动")
    return notes


def sync_buyer_commands(round_name: str) -> None:
    """Update executable hash-check examples, not historical performance notes."""
    valid_round(round_name)
    for name in (*BUYER_DOCS, "03-怎么发给买家.md", "README.md"):
        if Path(name).suffix != ".md":
            continue
        path = ROOT / name
        if not path.is_file():
            continue
        old = path.read_text(encoding="utf-8")
        new = re.sub(
            r"(certutil\s+-hashfile\s+)([\"']?)(小夜(?:-电脑\+手机)?-r)[0-9]+(\.zip)",
            lambda match: match.group(1)
            + match.group(2)
            + match.group(3)[:-1]
            + round_name
            + match.group(4),
            old,
        )
        if new != old:
            path.write_text(new, encoding="utf-8")
            print(f"  {name}：校验命令已同步到 {round_name}")


def stale_number_lines(round_name: str) -> list[str]:
    """对外文案里那些"带数字又写着别的轮次"的句子 —— 只点名，不自动改（改了可能把历史段落改错）。"""
    hits: list[str] = []
    pattern = re.compile(r"\bMB\b|\bMiB\b|sha256|certutil|个文件|字节|dist/小夜|解压后")
    for path in sorted(ROOT.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        for index, line in enumerate(text.splitlines(), start=1):
            if not pattern.search(line):
                continue
            for token in set(re.findall(r"\br\d{2,3}\b", line)):
                if token != round_name:
                    hits.append(f"{path.name}:{index} 还带着 {token} —— {line.strip()[:76]}")
    return hits


def package_name(round_name: str, with_mobile: bool) -> str:
    """合并包的文件名必须跟着**里面真装了什么**。

    这一行以前无条件写「小夜-电脑+手机-rNN.zip」。r122 因为手机端 APK 版本和源码不一致
    （盘上 0.4.2/10，`build.gradle` 已改到 0.5.0/11）只能先出电脑端，结果 10.3 GB 的包
    顶着「+手机」的名字、清单和发货记录也跟着印这个名字，而包内 APK 成员数是 0 ——
    买家按名字去找手机那一半会找不到。名字也是一种承诺。
    """
    return f"小夜-{'电脑+手机' if with_mobile else '电脑'}-{round_name}.zip"


def main() -> int:
    parser = argparse.ArgumentParser(description="做交付包（只做 dist/小夜，挡隐私、挡过期）")
    parser.add_argument("--round", default="auto", help="轮次目录名（例如 r76），auto = 最新一轮")
    parser.add_argument("--build-root", default=str(BUILD_ROOT))
    parser.add_argument("--out", default=str(ROOT / "交付包"))
    parser.add_argument("--with-mobile", action="store_true", help="带上最新手机 APK")
    parser.add_argument(
        "--with-docs", action="store_true", help="把买家说明拷进 交付包/文档/ 并装进包里"
    )
    parser.add_argument("--apk", type=Path, default=None, help="明确指定本次已核验的手机 APK")
    parser.add_argument(
        "--report", type=Path, default=None, help="原子写入本次产物/校验 JSON，供后续步骤读取"
    )
    parser.add_argument(
        "--pc-zip", action="store_true", help="另出一份只含电脑包的 小夜-rNN.zip（多花 8 分钟）"
    )
    parser.add_argument("--dry-run", action="store_true", help="只跑检查，一个字节都不写")
    parser.add_argument("--check", metavar="ZIP", help="只核对已经做好的 zip，不重压")
    parser.add_argument(
        "--skip-crc", action="store_true", help="核对时跳过整包 CRC（几分钟那段），只查目录和大小"
    )
    args = parser.parse_args()

    if args.check:
        if args.skip_crc:
            print("正式交付不允许跳过 CRC", file=sys.stderr)
            return 1
        target = Path(args.check).resolve()
        found = re.search(r"r[0-9]+", target.name)
        round_name = args.round if args.round != "auto" else (found.group(0) if found else "")
        try:
            valid_round(round_name)
            source = scoped_path(
                Path(args.build_root), Path(args.build_root) / round_name / "dist" / "小夜"
            )
            exe = source / "小夜.exe"
            if not target.is_file() or not exe.is_file():
                raise ValueError("缺少交付 ZIP 或对应 exe")
            started = time.time()
            bad = read_back(
                target,
                source,
                [p for p in source.rglob("*") if p.is_file()],
                exe,
                bundled_weights(source),
                [ROOT / name for name in BUYER_DOCS],
            )
            bad.extend(scan(source))
            bad.extend(freshness_warnings(source, exe))
            if args.apk is not None:
                with zipfile.ZipFile(target) as archive:
                    member = "手机/" + args.apk.name
                    if member not in archive.namelist():
                        bad.append("交付包缺少本次 APK")
                    else:
                        with archive.open(member) as handle:
                            packaged_hash = hashlib.file_digest(handle, "sha256").hexdigest()
                        if packaged_hash != digest_file(args.apk):
                            bad.append("交付包手机 APK 内容不一致")
            digest = digest_file(target)
            report = {
                "ok": not bad,
                "full_crc": True,
                "round": round_name,
                "zip": str(target),
                "sha256": digest,
                "size": target.stat().st_size,
                "source": str(source),
                "errors": bad,
                "apk": str(args.apk.resolve()) if args.apk else "",
                "apk_sha256": digest_file(args.apk) if args.apk else "",
                "exe_sha256": digest_file(exe),
                "source_sha256": source_fingerprint(REPO_ROOT),
            }
            if args.report is not None:
                write_json(args.report, report)
            for line in bad:
                print("不通过：" + line, file=sys.stderr)
            print(
                "独立回读 "
                f"{target.name}"
                "："
                f"{('不通过' if bad else '通过')}"
                " / SHA256="
                f"{digest}"
                " / "
                f"{time.time() - started:.0f}"
                "秒"
            )
            return 1 if bad else 0
        except (OSError, ValueError, zipfile.BadZipFile) as exc:
            print(f"交付校验失败：{exc}", file=sys.stderr)
            return 1

    build_root = Path(args.build_root)
    round_name = args.round
    if round_name == "auto":
        round_name = newest_round(build_root)
        if not round_name:
            print(f"{build_root} 下没有任何带 dist/小夜/小夜.exe 的 rNN 目录", file=sys.stderr)
            return 1
        print(f"--round auto → {round_name}")

    valid_round(round_name)
    source = scoped_path(build_root, build_root / round_name / "dist" / "小夜")
    exe = source / "小夜.exe"
    if not exe.is_file():
        print(
            f"找不到 {exe}\n先把这一轮打出来（PyInstaller --distpath .../{round_name}/dist）",
            file=sys.stderr,
        )
        return 1

    problems = scan(source)
    if problems:
        print("这个目录不是干净的装机包，拒绝打包。发现：", file=sys.stderr)
        for line in problems[:20]:
            print("  - " + line, file=sys.stderr)
        print(
            "\n如果是你自己双击运行过它留下的 `数据\\`：那**可能是你真实的对话库和记忆**，\n"
            "别删，把它整体移到 JarvisData 之类你自己的数据根下面再重跑本脚本。",
            file=sys.stderr,
        )
        return 1

    weights = bundled_weights(source)
    fake = weight_problems(weights)
    if fake:
        print("拒绝：模型目录里有不像真权重的文件。", file=sys.stderr)
        for line in fake:
            print("  - " + line, file=sys.stderr)
        print(
            "\n做实验留下的空文件（为了验内存门槛临时造的那两份）请删掉再重跑：\n"
            "它进了包，买家在下拉里看得见这个模型，点下去必定加载失败。",
            file=sys.stderr,
        )
        return 1
    if not weights:
        print(
            f"拒绝：{source / MODEL_DIRNAME} 里没有一个 .gguf。\n"
            f"商品文案现在写的是「随包带一个离线大模型、不要 Key」，包里没这东西就等于虚假发货。\n"
            f"要么把权重放进去（路径口径跟 jarvis/llm/local_server.py 一致），要么改文案再打。",
            file=sys.stderr,
        )
        return 1

    stale = freshness_warnings(source, exe)
    if stale:
        for line in stale:
            print("拒绝：" + line, file=sys.stderr)
        return 1

    apk = (args.apk or newest_apk(build_root)) if args.with_mobile else None
    if args.with_mobile and apk is None:
        print(
            f"{build_root / 'mobile'} 下没有 小夜-*.apk；先 build:mobile 或去掉 --with-mobile",
            file=sys.stderr,
        )
        return 1

    if apk is not None:
        try:
            mobile = read_json(apk.parent / "release.json")
            if not mobile.get("ok") or mobile.get("sha256") != digest_file(apk):
                raise ValueError("手机 APK 与已核验报告不一致")
            from release_manifest import mobile_fingerprint

            if mobile.get("mobile_source_sha256") != mobile_fingerprint(REPO_ROOT):
                raise ValueError("手机源码已变，必须重新构建并校验 APK")
        except (OSError, ValueError) as exc:
            print(f"拒绝：{exc}；先运行 verify_android_release.py", file=sys.stderr)
            return 1

    docs: list[Path] = []
    if args.with_docs:
        for name in BUYER_DOCS:
            src = ROOT / name
            if not src.is_file():
                print(f"缺买家文档 {name} —— 不猜内容，直接失败", file=sys.stderr)
                return 1
            docs.append(src)

    files = [p for p in source.rglob("*") if p.is_file()]
    total_bytes = sum(f.stat().st_size for f in files)
    out_dir = Path(args.out)
    # 包名跟着内容走，见 package_name() 那段（r122 踩的：不带 --with-mobile 却叫「电脑+手机」）。
    combined = out_dir / package_name(round_name, args.with_mobile)
    pc_zip = out_dir / f"小夜-{round_name}.zip"

    need = total_bytes + (DISK_HEADROOM_MB << 20)
    free = shutil.disk_usage(out_dir if out_dir.exists() else out_dir.parent).free
    if free < need:
        print(
            f"拒绝：{out_dir} 所在盘只剩 {human(free)}，这一包至少要 {human(need)}（源 {human(total_bytes)} + 余量）。\n"
            f"写一半的 5 GB 包比不写更糟：它看着像做完了。清盘或换 --out 指到别的盘。",
            file=sys.stderr,
        )
        return 1
    print(f"检查过了：源 {human(total_bytes)} / {len(files)} 个文件，盘上剩 {human(free)}")

    weight_md5 = {}
    for item in weights:
        print(f"随包权重：{MODEL_DIRNAME}\\{item.name} {human(item.stat().st_size)}，正在算 md5")
        weight_md5[item.name] = md5_of(item)
        print(f"  md5 = {weight_md5[item.name]}", flush=True)
    notice = model_notice(weights, round_name, weight_md5)
    if "END OF TERMS AND CONDITIONS" in notice and "APPENDIX: How to apply" in notice:
        print(f"许可与来源一页：Apache-2.0 全文已带上（{len(notice):,} 字符）")
    else:
        print(
            "⚠ 许可与来源一页里【没有】 Apache 全文（本机没找到标准文本），05 那句「全文随包」就不成立",
            file=sys.stderr,
        )

    if args.dry_run:
        print("--dry-run：一个字节都没写。上面几条检查就是真跑之前会过的关。")
        return 0

    sync_buyer_commands(round_name)
    head, dirty = git_state()
    manifest_lines = (
        [
            f"交付清单 · {now()}",
            f"git HEAD：{head}（工作区{'有未提交改动' if dirty else '干净'}）",
            f"构建轮次：{round_name}  exe {exe.stat().st_size:,} 字节  构建于 "
            f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(exe.stat().st_mtime))}",
            "",
            # 布局那一行也不许写死「+ 手机/」：不带 --with-mobile 时包里根本没有手机目录，
            # 买家照着清单去找会找不到（r122 同一处 bug 的第二半，前半是包名）。
            "包内布局：先看我一页.txt + 小夜/（双击 小夜/小夜.exe）"
            + ("+ 手机/ " if args.with_mobile else "")
            + "+ 文档/ + 清单.txt —— 解压一层就到位",
            *(
                f"⚠ 压缩包里 {name} 会显示成 {size % 2**32:,} 字节（真实 {size:,}）—— 32 位显示截断，"
                "不是包坏了；实测用 Windows 自带解压解出来的文件，大小和 md5 都和包里的一致"
                for name, size in over_4g(weights)
            ),
            "",
            f"随包大模型：{len(weights)} 份（在 {MODEL_DIRNAME} 目录里），许可依据逐份不同",
            "  （哪份头部写了什么、没写的依据是什么，见 小夜/模型/许可与来源.txt —— 那是从文件自己读出来的）",
            "  界面上「选哪个离线模型」列的就是下面这些；没选中的那个字节都不进内存：",
        ]
        + [
            f"    {item.name}  {item.stat().st_size:,} 字节  md5 {weight_md5[item.name]}"
            for item in weights
        ]
        + [
            "",
            "隐私检查：数据根 数据/ 不存在、exe 旁边没有 config.yaml/preferences.json/*.db/*.wav —— 通过",
            "（这一条只保证「这台机器上没跑过它」。买家用起来之后那些东西自然会出现在他自己机器上，那是他的。）",
        ]
    )
    for line in freshness_warnings(source, exe):
        manifest_lines.append(f"注意：{line}")
    if apk is not None:
        manifest_lines += [
            "",
            f"手机 APK：手机/{apk.name}  {human(apk.stat().st_size)}  sha256={sha256_of(apk)}",
            "  本次完成编译/签名/资源校验；真机未连接，不能据此声称通过 ROM/权限/通话验收。",
        ]
        dirty_mobile = mobile_source_dirty()
        if dirty_mobile:
            manifest_lines += [
                "  注意：手机源码在工作区有未提交改动，这个 APK 不一定等于当前源码 ——",
                "        " + "；".join(dirty_mobile[:6]),
            ]
    if docs:
        manifest_lines += ["", "随包文档："] + [
            f"  文档/{d.name}  sha256={sha256_of(d)}" for d in docs
        ]

    started = time.time()
    build_package(
        combined,
        source,
        files,
        apk,
        docs,
        round_name,
        ("\n".join(manifest_lines) + "\n").encode("utf-8"),
        weights,
        notice,
    )
    took = time.time() - started
    size = combined.stat().st_size
    digest = sha256_of(combined)
    print(f"合并包：{combined.name}  {human(size)}  （{took:.0f} 秒）")
    print(f"  SHA256 {digest}")

    bad = read_back(combined, source, files, exe, weights, docs)
    if args.skip_crc:
        print("  ⚠ 按 --skip-crc 跳过了整包 CRC 校验，只核对了目录和大小")
    for line in bad:
        print("  ✗ " + line, file=sys.stderr)
    if bad:
        print("这个包不能发 —— 回读不通过，看上面哪一条。", file=sys.stderr)
        return 1

    if args.pc_zip:
        started = time.time()
        pc_zip.parent.mkdir(parents=True, exist_ok=True)
        stats = [0, 0]
        with zipfile.ZipFile(pc_zip, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for item in files:
                add_file(archive, item, f"小夜/{item.relative_to(source)}", stats)
            archive.write(ROOT / "04-买家安装与使用说明.md", "小夜/买家安装与使用说明.md")
            archive.writestr(
                "小夜/授权.txt",
                f"小夜 · 构建 {round_name}\n一台机器自用；不得转售或二次分发。\n".encode(),
            )
            archive.write(ROOT / "05-第三方许可清单.md", "小夜/第三方许可清单.txt")
            archive.writestr(f"小夜/{MODEL_DIRNAME}/许可与来源.txt", notice)
            archive.writestr(
                "先看我一页.txt", buyer_first_page(round_name, None, weights, pc_zip.name)
            )
        print(
            f"电脑单包：{pc_zip.name}  {human(pc_zip.stat().st_size)}  （{time.time() - started:.0f} 秒）"
        )

    docs_dir = out_dir / "文档"
    if args.with_docs:
        docs_dir.mkdir(parents=True, exist_ok=True)
        for doc in docs:
            shutil.copy2(doc, docs_dir / doc.name)

    manifest_lines += [
        "",
        f"合并包 {combined.name}  {human(size)}  sha256={digest}  回读通过（{took:.0f} 秒压 + 校验）",
    ]
    if args.pc_zip:
        manifest_lines.append(
            f"电脑单包 {pc_zip.name}  {human(pc_zip.stat().st_size)}  sha256={sha256_of(pc_zip)}"
        )
    manifest_lines += [
        "",
        "发货前自己走一遍：",
        f"  1. 上传网盘（{human(size)}，限速要多久心里先有数）",
        "  2. 链接 + 提取码分两条消息发",
        f"  3. 把这条给买家：certutil -hashfile {combined.name} SHA256",
        "  4. 提醒他：压缩包里模型显示成 388 MB 是资源管理器截断，解压完才是 4.36 GB",
        "  5. 问一句他机器空闲内存有没有 5 GB，没有就按「要配 Key 的云端版」讲",
    ]
    manifest_path = out_dir / f"清单-{round_name}.txt"
    manifest_path.write_text("\n".join(manifest_lines) + "\n", encoding="utf-8")

    record_lines = [
        f"构建轮次：{round_name}",
        (
            "exe 大小："
            f"{exe.stat().st_size}"
            " 字节  mtime："
            f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(exe.stat().st_mtime))}"
        ),
        f"文件数：{len(files)}",
        f"源目录体积：{human(total_bytes)}",
        "随包模型："
        + "；".join(
            f"{item.name} {item.stat().st_size} 字节 md5={weight_md5[item.name]}"
            for item in weights
        ),
        f"压缩包：{combined.name}  {human(size)}",
        f"SHA256：{digest}",
        f"打包时间：{now()}  耗时 {took:.0f} 秒",
        f"买家校验：certutil -hashfile {combined.name} SHA256",
        "买家解压：Windows 自带解压实测也能解对；压缩包里显示 388 MB 是 32 位截断，不是坏包",
    ]
    if args.pc_zip:
        record_lines.append(
            f"电脑单包：{pc_zip.name}  {human(pc_zip.stat().st_size)}  sha256={sha256_of(pc_zip)}"
        )
    record = out_dir / "发货记录.txt"
    record.write_text("\n".join(record_lines) + "\n", encoding="utf-8")

    facts03 = "\n".join(
        [
            "| 项 | 数值 |",
            "|---|---|",
            f"| 交付目录 | `{round_name}/dist/小夜`（exe {exe.stat().st_size:,} 字节，构建 "
            f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(exe.stat().st_mtime))}） |",
            f"| 文件数 | {len(files)}（zip 里另加 先看我一页.txt / 授权 / 第三方许可清单 / 买家说明 / "
            f"模型许可 / 清单.txt 六条文本） |",
            f"| 源目录体积 | {human(total_bytes)}，其中随包权重 "
            + "、".join(
                f"`{item.name}` {item.stat().st_size:,} 字节（md5 `{weight_md5[item.name]}`）"
                for item in weights
            )
            + " |",
            f"| **压缩后** | 合并包 **{human(size)}**（{took:.0f} 秒，解压一层就到位）"
            + ("" if not args.pc_zip else f"；电脑单包 **{human(pc_zip.stat().st_size)}**")
            + " |",
            f"| SHA256 | 合并包 `{digest}`"
            + ("" if not args.pc_zip else f"<br>电脑单包 `{sha256_of(pc_zip)}`")
            + " |",
            "| 顶层内容 | `先看我一页.txt`、`小夜/`（exe + `买家安装与使用说明.md` + `授权.txt` + "
            "`第三方许可清单.txt` + `模型/许可与来源.txt`）、`手机/`、`文档/`、`清单.txt` |",
            "| 解压工具 | 7-Zip / Bandizip / WinRAR / Windows 自带的右键「全部解压」都行。只有**显示**有坑："
            + "、".join(
                f"压缩包里 `{name}` 显示成 {size % 2**32:,} 字节（真实 {size:,}）"
                for name, size in over_4g(weights)
            )
            + "，而实测解出来的文件是对的 —— 别照那个数字判断包坏了 |",
            "| 数据目录 | **0 条**。逐条扫过 zip 名字：没有 `数据/`、没有 `*.db`、没有 `preferences.json`；"
            "命中 `config.yaml` 的都在 `_internal/hydra/test_utils/configs/` 里，是 hydra 自带的测试样例 |",
        ]
    )
    facts_readme = "\n".join(
        [
            "| 产物 | 大小 | sha256 |",
            "|---|---|---|",
            f"| `{combined.name}`（发这个） | {human(size)} | `{digest}` |",
        ]
        + (
            [
                (
                    "| `"
                    f"{pc_zip.name}"
                    "`（电脑单包） | "
                    f"{human(pc_zip.stat().st_size)}"
                    " | `"
                    f"{sha256_of(pc_zip)}"
                    "` |"
                )
            ]
            if args.pc_zip
            else []
        )
        + (
            [
                f"| `{apk.name}`（手机，Beta 未真机验） | {human(apk.stat().st_size)} | `{sha256_of(apk)}` |"
            ]
            if apk is not None
            else []
        )
    )
    for line in stamp_docs(facts03 + "\n", facts_readme + "\n"):
        print("  " + line)

    stale = stale_number_lines(round_name)
    if stale:
        print(f"\n对外文案里还有 {len(stale)} 句「带数字 + 写着别的轮次」，逐条看一眼是不是旧话：")
        for line in stale[:12]:
            print("  · " + line)
        if len(stale) > 12:
            print(f"  …（另 {len(stale) - 12} 条）")

    others = sorted(
        (p for p in out_dir.glob("*.zip") if p != combined and (not args.pc_zip or p != pc_zip)),
        key=lambda p: p.stat().st_mtime,
    )
    if others:
        print("\n交付包里还躺着别的 zip（脚本**不自动删**，要删你点头）：")
        for path in others:
            print(
                "  · "
                f"{path.name}"
                "  "
                f"{human(path.stat().st_size)}"
                "  "
                f"{time.strftime('%m-%d %H:%M', time.localtime(path.stat().st_mtime))}"
            )
    print(f"\n清单 {manifest_path}\n发货记录 {record}")
    print(f"所在盘剩 {human(shutil.disk_usage(out_dir).free)}")
    if args.report is not None:
        write_json(
            args.report,
            {
                "ok": True,
                "full_crc": False,
                "round": round_name,
                "zip": str(combined.resolve()),
                "sha256": digest,
                "size": combined.stat().st_size,
                "source": str(source.resolve()),
                "apk": str(apk.resolve()) if apk else "",
            },
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
