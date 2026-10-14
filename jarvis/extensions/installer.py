"""安装：把一条搜索结果变成本机能用的东西，或者明确说不行。

两类货，两条完全不同的落盘路径：

- **技能** = 从 GitHub 取几份文本文件（`SKILL.md` + 随附的 `.md/.txt/.json/.yaml`）写进
  `数据/扩展/skills/<slug>/`。**不取二进制、不取脚本**：一个技能应该只有说明书，
  而"顺手把仓库里的 .py 也拉下来"是给下一步的执行留门 —— 这一步不留。
  写完立刻用技能层自己的解析器读一遍；读不过来就整目录删掉并说原因，
  不留下一个"看起来装好了"的半个技能。
- **MCP server** = 写 `servers.json` 里的一条。真正连接由装配那一侧做（`servers.merge` →
  `McpService`），安装器**不起进程**：装完能不能连上要在页面上看见，而不是在按钮点下去的
  那一秒悄悄试一次。

原子性同打包那条规矩：先 `.partial` 再 `os.replace`；中途失败不留半个文件。
"""

from __future__ import annotations

import hashlib
import json
import logging
import posixpath
import shutil
import time
import urllib.parse
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Final

from jarvis.core.constants import DEFAULT_ENCODING
from jarvis.extensions.runtime import RuntimeState, detect, missing_for
from jarvis.extensions.servers import ServerStoreError, load, path_for, save, upsert
from jarvis.extensions.sources import guarded_fetch
from jarvis.extensions.types import (
    KIND_MCP,
    KIND_SKILL,
    RAW_BASE,
    CatalogItem,
    InstallResult,
    string_list,
)
from jarvis.skills.loader import SKILL_FILENAME, read_directory
from jarvis.skills.service import SkillService

logger = logging.getLogger(__name__)

TEXT_SUFFIXES: Final = (".md", ".txt", ".json", ".yaml", ".yml")
"""只装这些后缀的随附文件。二进制/脚本一概不取，理由见模块开头。"""

MAX_FILES: Final = 20
MAX_TOTAL_BYTES: Final = 400_000
"""一个技能最多带 20 个文本文件、400 KB —— 超了就是"这不像一份说明书，这是一整个项目"。"""

SOURCE_FILENAME: Final = ".xiaoye-source.json"
"""装技能时留在它自己目录里的那份来历记录。

为什么要有：没有它，"上游有没有出新内容"这句话**无从判断**，"更新"那颗按钮就只能装样子。
为什么放目录里而不是集中的登记文件：拷走一个技能目录 = 连来历一起拷走，删掉目录 = 记录
跟着没；集中登记会出现"目录没了记录还在"那种对不上的账。
`discover()` 只扫子目录，所以这个文件不会被当成技能读进提示。
"""

Fetcher = Callable[[str, float], tuple[str, bytes]]


def safe_ref(ref: str) -> str:
    """分支名 / commit：只许字母数字点斜杠，且不许有 `..`。

    它会被拼进一个 URL 的**路径段**，`../anything` 在这种拼接里能把请求送到同一台主机的
    别的位置上去 —— 而 ref 是从远端清单来的，不是人打的。
    """
    raw = str(ref or "main").strip() or "main"
    if ".." in raw or not all(char.isalnum() or char in "._-/" for char in raw):
        raise ValueError(f"这个来源标记不像一个分支或 commit：{ref!r}")
    return raw[:80]


def safe_relpath(name: str) -> str:
    """目录内相对路径消毒：逐段拆、丢空段与点段、丢掉一切带分隔符的东西。

    GitHub 的 contents API 给的名字里出现 `../../` 是攻击面，不是怪事。
    """
    parts = [
        part
        for part in posixpath.normpath(str(name or "")).split("/")
        if part and part not in {".", ".."}
    ]
    cleaned = "/".join(parts).lstrip("/")
    if not cleaned or cleaned.startswith("\\") or ":" in cleaned:
        raise ValueError(f"这个文件名放不进技能目录：{name!r}")
    return cleaned[:180]


def skill_files(item: CatalogItem, fetch: Fetcher) -> list[tuple[str, bytes]]:
    """取回一个技能需要的文本文件。返回 [(相对路径, 字节)]，`SKILL.md` 必须在里面。"""
    spec = item.install if isinstance(item.install, Mapping) else {}
    repo = str(spec.get("repo") or "").strip()
    path = str(spec.get("path") or "").strip().strip("/")
    if not repo or "/" not in repo:
        raise ValueError("这条技能没有写明是哪个仓库（owner/name）")
    ref = safe_ref(str(spec.get("ref") or item.pinned or "main"))
    listing = _list_dir(repo, ref, path, fetch)
    # (从哪儿取, 写到技能目录里的哪儿)。**写到哪儿的那一段绝不能带 repo 的路径前缀**：
    # 带上的表现是 `扩展/skills/poster/skills/poster/SKILL.md` —— 文件都在，技能层却读不到，
    # 页面上是一颗"装好了"的按钮。
    # 取回路径带仓库里的子目录，写盘路径不带：装进 `扩展/skills/poster/` 的东西
    # 必须就叫 `SKILL.md`，否则技能层按 `<目录>/SKILL.md` 找不到它（本轮实测 404 抓出来的）。
    wanted: list[tuple[str, str]] = [
        (posixpath.join(path, SKILL_FILENAME) if path else SKILL_FILENAME, SKILL_FILENAME)
    ]
    for entry in listing:
        name = str(entry.get("name") or "") if isinstance(entry, Mapping) else ""
        if not name or str(entry.get("type") or "") != "file" or name == SKILL_FILENAME:
            continue
        if posixpath.splitext(name)[1].lower() not in TEXT_SUFFIXES:
            continue
        wanted.append((posixpath.join(path, name) if path else name, name))
    out: list[tuple[str, bytes]] = []
    total = 0
    for fetch_rel, write_rel in wanted[:MAX_FILES]:
        place = safe_relpath(write_rel)
        url = f"{RAW_BASE}{urllib.parse.quote(repo)}/{urllib.parse.quote(ref)}"
        url = f"{url}/{safe_relpath(fetch_rel)}"
        try:
            _, body = fetch(url, 10.0)
        except ValueError as exc:
            if place == SKILL_FILENAME:
                raise ValueError(f"取不到 SKILL.md：{exc}") from exc
            logger.info("技能随附文件没取到，跳过这一个：%s（%s）", place, exc)
            continue
        total += len(body)
        if total > MAX_TOTAL_BYTES:
            raise ValueError(f"这份技能加起来超过 {MAX_TOTAL_BYTES} 字节，不像一份说明书")
        out.append((place, body))
    if not any(rel == SKILL_FILENAME for rel, _ in out):
        raise ValueError("那份 SKILL.md 没取回来，所以什么都没写")
    return out


def _list_dir(repo: str, ref: str, path: str, fetch: Fetcher) -> list[Mapping[str, object]]:
    url = f"https://api.github.com/repos/{urllib.parse.quote(repo)}/contents/{path}?ref={urllib.parse.quote(ref)}"
    try:
        raw = json.loads(fetch(url, 10.0)[1].decode("utf-8", errors="replace"))
    except ValueError as exc:
        logger.info("列目录没成功（%s/%s@%s）：%s", repo, path, ref, exc)
        return []
    return [row for row in raw if isinstance(row, Mapping)] if isinstance(raw, list) else []


def tree_digest(files: list[tuple[str, bytes]]) -> str:
    """这一份取回的东西的合起来的指纹（文件名 + 每个文件的 sha256）。

    比"更新"用的是它，**不是版本号**：技能前言里写不写 `version` 全看作者心情，
    而"内容一模一样"这句话要成立，只能拿内容比。
    """
    digest = hashlib.sha256()
    for name, body in sorted(files):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(body).hexdigest().encode("ascii"))
    return digest.hexdigest()


def read_provenance(target: Path) -> dict[str, object]:
    """一个技能目录里的来历记录。没有 / 读不了都回空字典（调用方负责说清楚）。"""
    path = Path(target) / SOURCE_FILENAME
    if not path.is_file():
        return {}
    try:
        parsed = json.loads(path.read_text(encoding=DEFAULT_ENCODING))
    except (OSError, ValueError) as exc:
        logger.info("技能来历记录读不了（%s）：%s", path, exc)
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _provenance(
    item: CatalogItem, files: list[tuple[str, bytes]], slug: str, ref: str, digest: str
) -> dict[str, object]:
    spec = item.install if isinstance(item.install, Mapping) else {}
    return {
        "catalog_id": item.id,
        "kind": item.kind,
        "source": item.source,
        "name": item.name,
        "slug": slug,
        "repo": str(spec.get("repo") or ""),
        "ref": ref,
        "path": str(spec.get("path") or ""),
        "homepage": item.homepage,
        "installed_at": round(time.time(), 3),
        "digest": digest,
        "files": sorted(name for name, _ in files),
    }


def _write_provenance(target: Path, row: Mapping[str, object]) -> None:
    """写来历记录。它不是技能内容，所以**不算进"N 个文件"那句**。"""
    temp = target / (SOURCE_FILENAME + ".partial")
    temp.write_text(
        json.dumps(dict(row), ensure_ascii=False, indent=2) + "\n",
        encoding=DEFAULT_ENCODING,
        newline="\n",
    )
    temp.replace(target / SOURCE_FILENAME)


def _land(target: Path, files: list[tuple[str, bytes]]) -> list[Path]:
    """先写 `.partial` 再 replace：中途失败不留半个文件（和打包那条规矩一样）。"""
    written: list[Path] = []
    for rel, body in files:
        place = target / rel
        place.parent.mkdir(parents=True, exist_ok=True)
        temp = place.with_name(place.name + ".partial")
        temp.write_bytes(body)
        temp.replace(place)
        written.append(place)
    return written


def _skill_target(
    item: CatalogItem,
    skills: SkillService,
    fetch: Fetcher,
    audit: Callable[[str], None] | None,
    verb: str,
    *,
    prefetched: list[tuple[str, bytes]] | None = None,
) -> InstallResult:
    """取文件 → 落盘 → 读回来验一遍 → 记来历。装和更新走的是同一段，不抄两份。

    `prefetched` 是给"更新"那条路用的：它已经为了比内容取过一次，不该再取第二次
    （同一次点击打两遍网络，两遍之间上游还可能变）。
    """
    raw_path = item.install.get("path", "") if isinstance(item.install, Mapping) else ""
    target = skills.install_dir(str(raw_path))
    if not target.name:
        return InstallResult(False, item.kind, item.name, reason="没法从这条里取出一个目录名")
    # 顺序不能反：会失败的那几步（取文件、验 ref）必须在**动手拆盘上那一份之前**做完。
    # 反过来写的表现是"更新失败，但旧的那一份也没了"。
    try:
        files = prefetched if prefetched is not None else skill_files(item, fetch)
        spec = item.install if isinstance(item.install, Mapping) else {}
        ref = safe_ref(str(spec.get("ref") or "") or item.pinned or "main")
        digest = tree_digest(files)
    except ValueError as exc:
        return InstallResult(False, item.kind, item.name, reason=f"取文件失败：{exc}")
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)
    target.mkdir(parents=True, exist_ok=True)
    try:
        written = _land(target, files)
        manifest = read_directory(target)
        _write_provenance(target, _provenance(item, files, manifest.slug, ref, digest))
    except (OSError, ValueError) as exc:
        shutil.rmtree(target, ignore_errors=True)
        _note(audit, f"技能{verb}失败 {item.name}：{exc}")
        return InstallResult(
            False, item.kind, item.name, reason=f"这份技能{verb}完读不过来，已经回滚：{exc}"
        )
    skills.enable(manifest.slug)
    skills.reload()
    _note(
        audit,
        f"技能已{verb} {manifest.slug} ← {item.source}:{item.homepage or item.name}"
        f"（{len(written)} 个文件）",
    )
    return InstallResult(
        True,
        KIND_SKILL,
        manifest.name,
        path=str(target),
        detail=f"{len(written)} 个文件 · 正文 {manifest.body_chars} 字",
    )


def install_skill(
    item: CatalogItem,
    skills: SkillService,
    *,
    fetch: Fetcher | None = None,
    audit: Callable[[str], None] | None = None,
) -> InstallResult:
    """装一个技能。**先取全、后落盘、写完再读一遍**：任何一步失败都不留半个目录。"""
    do_fetch = fetch or guarded_fetch
    if item.kind != KIND_SKILL:
        return InstallResult(False, item.kind, item.name, reason="这一条不是技能")
    return _skill_target(item, skills, do_fetch, audit, "装上")


def update_skill(
    item: CatalogItem,
    skills: SkillService,
    *,
    fetch: Fetcher | None = None,
    audit: Callable[[str], None] | None = None,
) -> InstallResult:
    """按盘上那份来历记录重新取一次，**比内容**决定要不要动。

    三种结果都要说清楚，不能都折成"更新完成"：

    - 盘上没这一份 → 那是没装，不是更新失败；
    - 有目录但没来历记录（更早那版装的）→ **判断不了**，只能删掉重装。给一颗点了
      什么也不会变的按钮就是假能力，所以这里明确拒绝并说明；
    - 取回来一模一样 → 什么都不写（mtime 不变），并说"没动"。
    """
    do_fetch = fetch or guarded_fetch
    if item.kind != KIND_SKILL:
        return InstallResult(False, item.kind, item.name, reason="这一条不是技能")
    raw_path = item.install.get("path", "") if isinstance(item.install, Mapping) else ""
    target = skills.install_dir(str(raw_path))
    if not target.is_dir():
        return InstallResult(
            False, KIND_SKILL, item.name, path=str(target), reason="盘上没有这一份，先装再来更新"
        )
    before = read_provenance(target)
    if not before:
        return InstallResult(
            False,
            KIND_SKILL,
            item.name,
            path=str(target),
            reason="这一份没留下来历记录（多半是更早那版装的，那时候还没记）——"
            "所以这里判断不了上游有没有出新内容。要换新内容：先删掉这一份，再装一次。",
        )
    try:
        files = skill_files(item, do_fetch)
    except ValueError as exc:
        return InstallResult(False, item.kind, item.name, reason=f"取文件失败：{exc}")
    digest = tree_digest(files)
    if str(before.get("digest") or "") == digest:
        _note(audit, f"技能检查更新 {target.name}：来源和盘上这一份内容一样，没动文件")
        return InstallResult(
            True,
            KIND_SKILL,
            item.name,
            path=str(target),
            detail="没动：来源现在的内容和盘上这一份一模一样（比的是取回的全部文件，不是版本号）",
        )
    result = _skill_target(item, skills, do_fetch, audit, "更新", prefetched=files)
    if result.ok:
        return replace(result, detail=f"换成新内容了（{len(files)} 个文件）。" + result.detail)
    return result


def install_mcp(
    item: CatalogItem,
    extensions_dir: Path,
    *,
    environ: Mapping[str, str] | None = None,
    runtime: RuntimeState | None = None,
    audit: Callable[[str], None] | None = None,
) -> InstallResult:
    """写一条 MCP server 进 `servers.json`（装 = 记录配置；连接是另一件事，页面上看得见）。

    运行环境先查：缺 `npx` 的时候不给装 npm 那一条 —— 装上就是一条永远连不上的记录，
    而那正是这个项目被反复要求停止生产的东西。
    """
    if item.kind != KIND_MCP:
        return InstallResult(False, item.kind, item.name, reason="这一条不是 MCP server")
    state = runtime if runtime is not None else detect()
    present = list(state["present"])
    gaps = missing_for(tuple(item.needs), present)
    if gaps:
        return InstallResult(
            False,
            KIND_MCP,
            item.name,
            reason="这台机器上缺 "
            + "、".join(gaps)
            + f"，装上了也起不来（{item.needs} 是这一条要的东西）",
        )
    spec = item.install if isinstance(item.install, Mapping) else {}
    env_from = string_list(spec.get("env_from"))
    _known, absent = _resolve_for_report(env_from, environ)
    if absent:
        return InstallResult(
            False,
            KIND_MCP,
            item.name,
            reason="这条需要环境变量 " + "、".join(absent) + "，这台机器上没有。"
            "密钥不写进扩展文件（那是会被打包、被同步的东西），请先设成环境变量再来装。",
        )
    path = path_for(extensions_dir)
    try:
        data = load(path)
        merged, name = upsert(
            data,
            {
                **dict(spec),
                "name": spec.get("name") or item.name,
                "source": item.source,
                "homepage": item.homepage,
                "needs": list(item.needs),
                "env_from": env_from,
                "note": item.summary[:200],
            },
        )
        save(path, merged)
    except (ServerStoreError, ValueError) as exc:
        return InstallResult(False, KIND_MCP, item.name, reason=f"这条写不进去：{exc}")
    _note(
        audit,
        f"MCP server 已登记 {name}（{spec.get('command') or spec.get('url')}）← {item.source}",
    )
    return InstallResult(
        True,
        KIND_MCP,
        name,
        path=str(path),
        detail="已登记；下一轮对话起她就能用它的工具（连接状态在同一页下面那格）",
    )


def _resolve_for_report(
    names: list[str], environ: Mapping[str, str] | None
) -> tuple[list[str], list[str]]:
    from jarvis.extensions.servers import resolve_env

    given, missing = resolve_env(tuple(names), environ)
    return sorted(given), missing


def _note(audit: Callable[[str], None] | None, text: str) -> None:
    if audit is None:
        return
    try:
        audit(text)
    except Exception:
        logger.warning("扩展审计写不进去：%s", text, exc_info=True)


__all__ = [
    "MAX_FILES",
    "MAX_TOTAL_BYTES",
    "SOURCE_FILENAME",
    "TEXT_SUFFIXES",
    "install_mcp",
    "install_skill",
    "read_provenance",
    "safe_ref",
    "safe_relpath",
    "skill_files",
    "tree_digest",
    "update_skill",
]
