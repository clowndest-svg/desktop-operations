"""有没有网这一格：探测必须在读路径之外，"没网"必须是真的都没应。

两条判据都是被别的坑教出来的：面板每 1.5 秒读一次快照（探测放进读路径 = 每 1.5 秒卡一次
界面），以及一次连接失败就说"没网"（DNS 挂了和出口断了不是一回事，用户要做的事也不同）。
"""

from __future__ import annotations

import re
from typing import Any

from jarvis.app.net_state import DEFAULT_TARGETS, NetworkWatch

TARGETS = (("阿里 DNS", "223.5.5.5", 53), ("腾讯 DNS", "119.29.29.29", 53))


class Watcher:
    """替掉真 socket：记着探了几次、每次是谁答的话。"""

    def __init__(self, *, answering: set[str] | None = None) -> None:
        self.answering = answering if answering is not None else {"阿里 DNS"}
        self.calls = 0

    def __call__(self, target: tuple[str, str, int], timeout: float) -> tuple[str, bool]:
        del timeout
        self.calls += 1
        return target[0], target[0] in self.answering


class Clock:
    """一根可以自己拨的表：测"这份读数是几秒前的"不能靠真等。"""

    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def _watch(probe: Any, clock: Clock | None = None) -> tuple[NetworkWatch, Clock]:
    face = clock or Clock()
    watch = NetworkWatch(TARGETS, probe=probe, clock=face)
    return watch, face


def test_the_default_targets_include_bare_ips_as_well_as_a_hostname() -> None:
    """只探域名的话，DNS 挂了和出口断了会被说成同一句话。"""
    labels = [target[1] for target in DEFAULT_TARGETS]
    bare_ips = [item for item in labels if re.fullmatch(r"(\d{1,3}\.){3}\d{1,3}", item)]
    assert len(bare_ips) >= 2, f"探针里裸 IP 太少，DNS 挂了会被误报成没网：{labels}"
    assert len(set(labels)) > len(bare_ips), "也要有一个走域名的目标"


def test_state_before_the_first_probe_says_it_does_not_know() -> None:
    watch, _ = _watch(Watcher())
    assert watch.state() == {"online": None, "detail": "还没测过", "seconds_ago": None}


def test_reading_the_state_never_probes() -> None:
    """这一格会被 1.5 秒一次的轮询读到 —— 探测放进这里等于每 1.5 秒卡一次界面。"""
    probe = Watcher()
    watch, _ = _watch(probe)
    watch.check()
    probe.calls = 0
    for _ in range(200):
        assert watch.state()["online"] is True
    assert probe.calls == 0, "state() 偷偷探了一次网"


def test_one_answer_is_enough_to_call_it_online_and_names_the_rest() -> None:
    probe = Watcher(answering={"腾讯 DNS"})
    watch, _ = _watch(probe)
    state = watch.check()
    assert state["online"] is True
    assert "腾讯 DNS" in str(state["detail"]) and "阿里 DNS" in str(state["detail"]), "不通的要点名"


def test_offline_needs_every_probe_to_fail() -> None:
    probe = Watcher(answering=set())
    watch, _ = _watch(probe)
    state = watch.check()
    assert state["online"] is False and "没有一个探针应答" in str(state["detail"])
    assert probe.calls == len(TARGETS)


def test_the_last_answer_is_kept_with_its_age() -> None:
    """翻成"没网"之后，界面上还要能看出这份读数是几秒前的。"""
    watch, clock = _watch(Watcher())
    watch.check()
    assert watch.state()["seconds_ago"] == 0.0
    clock.now = 121.5
    assert watch.state()["seconds_ago"] == 21.5
    assert watch.state()["online"] is True, "探不到新答案时别把上一次擦掉"
