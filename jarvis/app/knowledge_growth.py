"""她这一轮真从网上查过的东西，自动存进自己的知识库。

**为什么只认"真上过网"的那一轮**：用户要的是"根据对话和联网搜索充盈知识库"。纯对话那一半
已经有着落 —— 他说"记一下"时走 `add_knowledge` / `remember`（r89 补的写半）。剩下的"每轮聊天
都自动提炼成事实"要额外叫一次模型判断，而随包 4B 一次 130~300 秒，等于把每轮问答拖成两倍；
更要紧的是 r88 那次「离线啥都问不了」的三条真因里就有一条是**资料库提示污染**。所以这一层
只在**这一轮真的调过 `search_web` / `fetch_url`** 时落盘：有来源、有时间、可核对、可整批删。

**为什么必须看得见**：自动写进知识库的东西，如果人看不见也删不掉，那就不是资料库，
是她的第二个大脑而只有她有钥匙。所以条目单独落在 `数据/笔记/_自学/`（`notes_dir` 就是
「数据/笔记」—— 别写成「数据/知识库」，那是另一个东西），面板分组列，
一颗「只删她自学的」，开关在设置里，一天存多少条有上限，存失败要在屏上说话。
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final, Protocol, TypedDict

from jarvis.app.preferences import (
    KNOWLEDGE_AUTO_COUNT,
    KNOWLEDGE_AUTO_LEARN,
    KNOWLEDGE_AUTO_SEEN,
)
from jarvis.app.web_research import public_sources

if TYPE_CHECKING:  # pragma: no cover - 只为类型检查
    from jarvis.knowledge.service import KnowledgeService

logger = logging.getLogger(__name__)

LEARNED_FOLDER: Final = "_自学"
"""自学条目落的子目录，在 `notes_dir`（= `数据/笔记`）里面。

    路径本身就是分类依据，不再另建一张表 —— 但也就此记下：屏幕上要印完整路径必须问后端要，
    不能由界面拼一个"看起来对"的目录名。"""

LEARNED_SOURCE: Final = "自学·联网搜索"
WEB_TOOLS: Final = frozenset({"search_web", "fetch_url"})
MAX_BODY: Final = 4000
"""一条笔记最多带多少字的搜索回话。全文进库会把检索结果淹成一片。"""
MAX_HITS: Final = 5
MAX_SEEN: Final = 400
"""去重清单的长度上限：超了就丢最旧的（宁可重存一条，也不让一个键无限长）。"""

AUTO_LEARN: Final = KNOWLEDGE_AUTO_LEARN
AUTO_SEEN: Final = KNOWLEDGE_AUTO_SEEN
AUTO_COUNT: Final = KNOWLEDGE_AUTO_COUNT
"""键名定义在 `jarvis/app/preferences.py` 的 `PREFERENCE_TYPES` 旁边 —— 那里是登记表，
没登记的键 `set()` 会**静默拒绝**（只留一行日志和一个 False）。"""


class PreferencesSurface(Protocol):
    """``jarvis.app.preferences.Preferences`` 里这一层用到的那几样。

    `get` 直接返回 JSON 里的原值（列表/字典都行），所以清单不需要自己 dumps 成字符串。
    """

    def flag(self, key: str, *, default: bool = ...) -> bool: ...

    def get(self, key: str, default: object = ...) -> object: ...

    def set(self, key: str, value: object) -> bool: ...

    def set_flag(self, key: str, value: bool) -> bool: ...


class GrowthOutcome(TypedDict):
    stored: bool
    reason: str
    title: str
    """为什么没存 —— 每一句都要能直接念给人听，不许是 "skipped"。"""


@dataclass(frozen=True)
class TurnEvidence:
    """一轮问答里，这一层需要看到的四样。"""

    question: str
    answer: str
    tools: Sequence[str] = ()
    web_results: Sequence[str] = ()


_PUNCT = "？?！!。.，,、；;：:（）()【】[]《》〈〉“”\"'’·—-…~ \t\r\n　"
_DROP = str.maketrans(dict.fromkeys(_PUNCT))
"""指纹只留字面意思：标点全抹。第一次判据就写松了 —— 只去空白的版本里
「北京今天天气如何？」和「北京今天天气如何」算两条，同一个问题问两遍就存两份，
两份的搜索时间不同，检索时一起端给模型。"""


def fingerprint(question: str) -> str:
    cleaned = str(question or "").translate(_DROP).lower()
    return hashlib.sha256(cleaned.encode("utf-8")).hexdigest()[:16]


class KnowledgeGrowth:
    """把"查过的东西"变成"存下来的东西"，并且说清每一次为什么没存。"""

    def __init__(
        self,
        knowledge: KnowledgeService | None,
        prefs: PreferencesSurface,
        *,
        clock: Callable[[], float] = time.time,
        limit_per_day: int = 20,
        min_answer_chars: int = 120,
    ) -> None:
        self._knowledge = knowledge
        self._prefs = prefs
        self._clock = clock
        self._limit = max(1, int(limit_per_day))
        self._min_answer = max(1, int(min_answer_chars))
        self._write_lock = threading.Lock()

    # ------------------------------------------------------------------
    # 开关与账目
    # ------------------------------------------------------------------

    def enabled(self) -> bool:
        """默认**开**。关掉这条能力就看不见，和"注册一条永远失败的路"是同一类问题；
        但界面上必须有这颗开关，而且默认值要写在屏上。"""
        return self._prefs.flag(AUTO_LEARN, default=True)

    def set_enabled(self, on: bool) -> bool:
        self._prefs.set_flag(AUTO_LEARN, bool(on))
        return self.enabled()

    def stats(self) -> dict[str, object]:
        """面板那一格：开了没有、今天存了几条、上限多少、清单里一共记了几条、存在哪。

        `directory` 必须由后端给：上一版界面想显示路径却只能自己拼，而真路径是
        `数据/笔记/_自学`（`notes_dir` 不叫"知识库"），拼出来那句是假的。
        """
        directory = ""
        if self._knowledge is not None:
            notes = getattr(self._knowledge, "notes_dir", None)
            if notes is not None:
                directory = str(Path(str(notes)) / LEARNED_FOLDER)
        return {
            "directory": directory,
            "enabled": self.enabled(),
            "today": self._today_count(),
            "limit_per_day": self._limit,
            "total_seen": len(self._seen_list()),
            "folder": LEARNED_FOLDER,
            "has_knowledge": self._knowledge is not None and self._knowledge.enabled,
        }

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------

    def consider(self, evidence: TurnEvidence) -> GrowthOutcome:
        with self._write_lock:
            return self._consider(evidence)

    def _consider(self, evidence: TurnEvidence) -> GrowthOutcome:
        """决定这一轮要不要存。任何一条不满足就**明说原因**，不静默跳过。"""
        if self._knowledge is None:
            return self._no("这台机器上没有知识库这一层")
        if not self._knowledge.enabled:
            return self._no("知识库没启用（设置里 knowledge.enabled 是关的）")
        if not self.enabled():
            return self._no("自动学习这一档被关掉了（设置 → 助手 → 她自学）")
        asked = str(evidence.question or "").strip()
        answer = str(evidence.answer or "").strip()
        if not asked:
            return self._no("这一轮没有问题原文，存进去也没法回答是谁问的")
        if not any(name in WEB_TOOLS for name in evidence.tools):
            return self._no("这一轮没上网查，没有需要她记的新东西")
        if not any(
            public_sources(hit) and not hit.startswith(("工具 ", "[出错]", "[请求失败]"))
            for hit in evidence.web_results
        ):
            return self._no("没有可核对的网页来源，不把模型回答写成事实")
        if len(answer) < self._min_answer:
            return self._no(f"回答只有 {len(answer)} 字，太短不值得存成一条资料")
        if answer.startswith(("[已停止]", "[请求失败]", "[出错]")):
            return self._no("这一轮没答上，不能把失败存成事实")
        stamp = self._day() + ":" + fingerprint(asked)
        seen = self._seen_list()
        if stamp in seen:
            return self._no("这个问题她之前已经存过一次了")
        today = self._today_count()
        if today >= self._limit:
            return self._no(f"今天已经自学 {today} 条，到上限了（明天再继续，或去设置里调）")

        title = f"自学：{asked[:40]}"
        body = self._page(asked, answer, evidence.web_results)
        result = self._knowledge.ingest_text(
            title, body, source=LEARNED_SOURCE, folder=LEARNED_FOLDER
        )
        refused = str(getattr(result, "error", "") or getattr(result, "skipped_reason", "") or "")
        if refused or not str(getattr(result, "doc_id", "") or ""):
            logger.warning("自学条目没存进去：%s", refused or "知识库没回 doc_id")
            return self._no(f"没存下：{refused or '知识库没答应'}")
        self._prefs.set(AUTO_SEEN, [*seen, stamp][-MAX_SEEN:])
        self._prefs.set(AUTO_COUNT, {"day": self._day(), "count": today + 1})
        logger.info("她自学了一条：%s", title)
        return {"stored": True, "reason": "", "title": title}

    # ------------------------------------------------------------------
    # 一键清掉她自学的
    # ------------------------------------------------------------------

    def forget_learned(self) -> int:
        """删掉落在 `LEARNED_FOLDER` 里的那些条目（索引 + 文件），人喂的资料一条都不动。

        只删索引不删文件会留下"下次导入又回来了"的坑，所以两边一起删；
        删不掉的（文件被占用）要写日志说出来，不许假装清零。
        """
        if self._knowledge is None:
            return 0
        removed = 0
        for row in self._knowledge.documents():
            data = dict(row)
            source = str(data.get("source") or "")
            doc_id = str(data.get("doc_id") or "")
            if not doc_id or not self._is_learned(source):
                continue
            if self._knowledge.forget(doc_id):
                removed += 1
                self._unlink(source)
            else:
                logger.warning("这条自学条目删不掉（索引没答应）：%s", source)
        self._prefs.set(AUTO_SEEN, [])
        self._prefs.set(AUTO_COUNT, {"day": self._day(), "count": 0})
        logger.info("清掉她自学的 %d 条", removed)
        return removed

    @staticmethod
    def _is_learned(source: str) -> bool:
        normalized = str(source or "").replace("\\", "/")
        return f"/{LEARNED_FOLDER}/" in normalized

    @staticmethod
    def _unlink(source: str) -> None:
        with contextlib.suppress(OSError):
            Path(source).unlink(missing_ok=True)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    @staticmethod
    def _no(reason: str) -> GrowthOutcome:
        return {"stored": False, "reason": reason, "title": ""}

    def _page(self, asked: str, answer: str, hits: Sequence[str]) -> str:
        when = datetime.fromtimestamp(self._clock()).strftime("%Y-%m-%d %H:%M")
        lines = [
            f"问：{asked}",
            "",
            "仅收录本轮检索来源。模型回答留在聊天历史，不将它自动提升为事实。",
            "",
            f"依据（{when} 真从网上查的，可能已经过期）：",
        ]
        for hit in list(hits)[:MAX_HITS]:
            text = str(hit or "").strip()[:1200]
            if text:
                lines.append(f"- {text}")
        if not any(str(hit or "").strip() for hit in hits[:MAX_HITS]):
            lines.append("- （这一轮没留下搜索回话原文，只有上面那段回答）")
        lines.append("")
        lines.append("（这条是她自己存的，不是人喂的资料。设置 → 助手 → 她自学 可以关掉。）")
        return "\n".join(lines)

    def _seen_list(self) -> list[str]:
        raw = self._prefs.get(AUTO_SEEN, [])
        if not isinstance(raw, list):
            return []
        return [str(item) for item in raw]

    def _day(self) -> str:
        return datetime.fromtimestamp(float(self._clock())).strftime("%Y-%m-%d")

    def _today_count(self) -> int:
        raw = self._prefs.get(AUTO_COUNT, None)
        if isinstance(raw, dict) and str(raw.get("day") or "") == self._day():
            try:
                return int(raw.get("count") or 0)
            except (TypeError, ValueError):
                return 0
        return 0


__all__ = [
    "AUTO_COUNT",
    "AUTO_LEARN",
    "AUTO_SEEN",
    "LEARNED_FOLDER",
    "LEARNED_SOURCE",
    "GrowthOutcome",
    "KnowledgeGrowth",
    "TurnEvidence",
    "fingerprint",
]
