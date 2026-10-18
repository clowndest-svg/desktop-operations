"""新能力的测试：模型选择器、建议器（AI + 规则两条路）、Qwen3 的思考开关。

写这些的规矩和之前一样：每条都要能被"改回看起来也对的写法"抓住，所以断言的是数字与句子，
不是"没抛异常"。
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from jarvis.app import local_advice
from jarvis.app.chat_service import ChatService, ThinkingRequest, _Pair
from jarvis.app.local_advice import Machine, parse_advice, rule_advice
from jarvis.app.preferences import (
    LLM_OVERRIDES,
    LOCAL_CONTEXT,
    LOCAL_IDLE_MINUTES,
    LOCAL_MODEL,
    Preferences,
)
from jarvis.app.settings_service import SettingsService
from jarvis.llm import local_server
from jarvis.llm.local_server import MODEL_CARDS, ModelCard
from tests._fakes import json_object
from tests.test_app_settings_service import _FakeLlm, _section

REPO = Path(__file__).resolve().parents[1]


def _card(name: str = "Qwen3-4B.Q4_K_M.gguf", **over: Any) -> ModelCard:
    base = {
        "file": name,
        "label": "Qwen3-4B",
        "alias": "qwen3-4b",
        "license": "Apache-2.0",
        "gate_base_mb": 3200,
        "kv_mb_per_1k": 80,
        "tiers": {4096: (2828, 2827), 8192: (2961, 3137)},
        "acts_verified": True,
        "thinking": True,
    }
    base.update(over)
    return ModelCard(**base)  # type: ignore[arg-type]


def _row(
    file: str, size_mb: int, gate: int, *, present: bool = True, acts: bool = False
) -> dict[str, object]:
    wide = gate + 200
    return {
        "file": file,
        "label": file.split(".")[0],
        "size_mb": size_mb,
        "present": present,
        "selected": False,
        "license": "Apache-2.0",
        "measured": True,
        "thinking": False,
        "acts_verified": acts,
        "gate_mb": gate,
        "resident_mb": gate,
        "cpu_mb": gate,
        "tiers": [
            {"context": 4096, "needs_free_mb": gate, "resident_mb": gate},
            {"context": 8192, "needs_free_mb": wide, "resident_mb": wide},
        ],
    }


@pytest.fixture(autouse=True)
def _restore_tuning() -> Iterator[None]:
    """这一文件的测试直接拧 `_TUNING`，跑完必须放回去。

    漏出去的表现很坑：下一个文件里那个"假货架"会拿着残留的模型名去货架上找，找不到就报
    「选的那个模型的文件不在包里」，于是一堆跟旋钮无关的测试一起红 —— 而单跑每个文件都是绿的。
    """
    before = local_server.tuning()
    yield
    local_server.configure(before)


# ------------------------------------------------------------------ 选择器（llm 层）
def test_picking_none_says_that_instead_of_blaming_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(local_server, "runtime_dir", lambda: tmp_path)
    local_server.configure(local_server.Tuning(model=local_server.MODEL_NONE))

    verdict = local_server.plan()

    assert verdict.can_run is False
    assert "不用离线模型" in verdict.reason
    assert "内存" not in verdict.reason, "明明是人自己关的，不能报成内存不够"


def test_a_picked_file_that_is_not_there_names_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(local_server, "runtime_dir", lambda: tmp_path)
    monkeypatch.setattr(local_server, "installed_models", lambda: [tmp_path / "别的.gguf"])
    local_server.configure(local_server.Tuning(model="Qwen3-4B.Q4_K_M.gguf"))

    verdict = local_server.plan()

    assert verdict.can_run is False
    assert "Qwen3-4B.Q4_K_M.gguf" in verdict.reason


def test_the_gate_follows_the_picked_model_not_the_biggest_one() -> None:
    """选 0.6B 就不该被 7B 的 5.2 GB 门槛挡住 —— 这正是"按电脑选模型"的意义。"""
    small = local_server.card_for(Path("Qwen3-0.6B-Q8_0.gguf"))
    big = local_server.card_for(Path("Qwen2.5-7B-Instruct.Q4_K_M.gguf"))

    assert local_server.gate_mb(small, 4096) == 1200
    assert local_server.gate_mb(big, 4096) == 5200
    assert local_server.gate_mb(small, 16384) == 1920, "小模型的门槛要按它自己的 KV 涨"


def test_the_plan_prices_the_picked_model_not_the_default_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """走一遍真 plan()：选 0.6B 的机器不该被出厂 7B 的 5.2 GB 门槛挡在门外。"""
    runtime = tmp_path / "llama"
    runtime.mkdir()
    (runtime / local_server.SERVER_EXE).write_bytes(b"MZ")
    monkeypatch.setattr(local_server, "runtime_dir", lambda: runtime)
    monkeypatch.setattr(local_server, "installed_models", lambda: [Path("Qwen3-0.6B-Q8_0.gguf")])
    monkeypatch.setattr(local_server, "free_memory_mb", lambda: 1500)
    local_server.configure(local_server.Tuning(model="Qwen3-0.6B-Q8_0.gguf"))

    verdict = local_server.plan()

    assert verdict.needed_mb == 1200, verdict.needed_mb
    assert verdict.can_run is True, verdict.reason

    local_server.configure(local_server.Tuning(model="Qwen2.5-7B-Instruct.Q4_K_M.gguf"))
    monkeypatch.setattr(
        local_server, "installed_models", lambda: [Path("Qwen2.5-7B-Instruct.Q4_K_M.gguf")]
    )
    big = local_server.plan()
    assert big.needed_mb == 5200
    assert big.can_run is False and "内存" in big.reason


def test_an_unmeasured_file_is_priced_by_size_and_says_so(tmp_path: Path) -> None:
    fake = tmp_path / "someone-elses.gguf"
    fake.write_bytes(b"x" * (200 * 1024 * 1024))

    card = local_server.card_for(fake)

    assert card.measured is False
    assert 700 < card.gate_base_mb < 800, card.gate_base_mb
    assert card.gate_base_mb < 5200, "200 MB 的文件不该按 7B 的门槛拒"
    assert card.acts_verified is False


def test_a_vanished_file_does_not_make_the_catalog_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """model_file 可以被 mock 成一个不存在的名字（测试与升级中的真实状态都这样）。"""
    monkeypatch.setattr(local_server, "model_file", lambda: tmp_path / "gone.gguf")

    card = local_server.card_for(local_server.model_file())

    assert card.measured is False


def test_the_catalog_marks_the_auto_pick_and_the_missing_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    big = Path("Qwen2.5-7B-Instruct.Q4_K_M.gguf")
    small = Path("Qwen3-0.6B-Q8_0.gguf")
    monkeypatch.setattr(local_server, "tuning", lambda: local_server.Tuning())
    monkeypatch.setattr(local_server, "installed_models", lambda: [big])

    rows = {str(row["file"]): row for row in local_server.bundled_catalog()}

    assert rows["Qwen2.5-7B-Instruct.Q4_K_M.gguf"]["selected"] is True
    assert rows["Qwen3-0.6B-Q8_0.gguf"]["selected"] is False
    assert rows["Qwen3-0.6B-Q8_0.gguf"]["present"] is False
    assert rows["Qwen3-0.6B-Q8_0.gguf"]["size_mb"] == 0
    assert rows[small.name]["gate_mb"] == 1200, "文件不在也要报得出它要多少内存"


def test_reading_the_catalog_starts_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """面板每次打开都读它 —— 读一次分配 5 GB 是最坏的那种惊喜。"""

    def boom() -> list[Path]:
        raise AssertionError("读目录不许起进程，也不许碰运行时")

    monkeypatch.setattr(local_server, "runtime_dir", boom)
    monkeypatch.setattr(local_server, "installed_models", lambda: [])

    rows = local_server.bundled_catalog()

    assert rows, "出厂档案要一直列得出来，哪怕文件都不在"
    assert all(row["present"] is False and row["size_mb"] == 0 for row in rows)


# ------------------------------------------------------------------ 建议器
def test_the_rule_engine_picks_the_biggest_that_fits() -> None:
    machine = Machine("x", 12, 16384, 5000, 500_000, "GTX 1650", 4096)
    catalog = [
        _row("a.gguf", 4466, 5200),
        _row("b.gguf", 2590, 3200, acts=True),
        _row("c.gguf", 610, 1200),
    ]

    advice = rule_advice(machine, catalog)

    assert advice.model == "b.gguf", "空闲 5000 / 1.25 余量 = 4000 MB，只有 b 装得下"
    assert advice.source == "rule"


def test_the_rule_engine_says_none_when_nothing_fits() -> None:
    machine = Machine("x", 4, 8192, 900, 500_000, "", 0)
    catalog = [_row("a.gguf", 4466, 5200), _row("c.gguf", 610, 1200)]

    advice = rule_advice(machine, catalog)

    assert advice.model == local_server.MODEL_NONE
    assert "云端" in advice.reason


def test_the_rule_engine_opens_the_window_only_for_a_model_seen_acting() -> None:
    roomy = Machine("x", 12, 32768, 24000, 500_000, "RTX 4060", 8192)
    acts = [_row("a.gguf", 4466, 5200, acts=True)]
    unverified = [_row("a.gguf", 4466, 5200, acts=False)]

    assert rule_advice(roomy, acts).context == 8192
    assert rule_advice(roomy, unverified).context == 4096, "没见过它动手就别把话说满"


def test_advice_off_the_list_is_refused_rather_than_saved() -> None:
    catalog = [_row("a.gguf", 4466, 5200)]

    with pytest.raises(ValueError, match="不在档位"):
        parse_advice(
            '{"model": "a.gguf", "context": 3000, "idle_minutes": 10, "eager": false}', catalog
        )
    with pytest.raises(ValueError, match="这台机器上没有"):
        parse_advice(
            '{"model": "别的.gguf", "context": 4096, "idle_minutes": 10, "eager": false}', catalog
        )
    with pytest.raises(ValueError, match="JSON"):
        parse_advice("我觉得用 4B 挺好的", catalog)


def test_a_model_that_wraps_the_json_in_a_fence_still_parses() -> None:
    catalog = [_row("a.gguf", 4466, 5200)]
    body = '{"model": "a.gguf", "context": 8192, "idle_minutes": 5, "eager": true}'
    text = "```json\n" + body + "\n```"

    advice = parse_advice(text, catalog)

    assert (advice.context, advice.idle_minutes, advice.eager, advice.source) == (
        8192,
        5,
        True,
        "ai",
    )


# ------------------------------------------------------------------ 设置服务
@pytest.fixture()
def prefs(tmp_path: Path) -> Preferences:
    return Preferences(tmp_path / "preferences.json")


def _service(prefs: Preferences, ask: Any = None) -> SettingsService:
    service = SettingsService(prefs, _FakeLlm(), lambda: _section(), environ={})  # type: ignore[arg-type]
    if ask is not None:
        service.set_advice_ask(ask)
    return service


def test_picking_a_model_that_is_not_in_the_package_is_refused(prefs: Preferences) -> None:
    service = _service(prefs)

    answer = service.apply({"local_model": "没这个.gguf"})

    assert "没这个.gguf" in str(answer["problems"]["local_model"]), answer
    assert prefs.text(LOCAL_MODEL, default="") == "", "被拒的值不该留下半个"


def test_the_tier_numbers_change_with_the_picked_model(
    prefs: Preferences, monkeypatch: pytest.MonkeyPatch
) -> None:
    """下拉里那句"要约几 GB"必须跟着选中的模型走：0.6B 用 7B 的门槛会白拒一台小机器。"""
    monkeypatch.setattr(local_server, "selected_card", lambda: MODEL_CARDS["Qwen3-0.6B-Q8_0.gguf"])
    monkeypatch.setattr(local_server, "model_file", lambda: Path("Qwen3-0.6B-Q8_0.gguf"))
    service = _service(prefs)

    rows = {
        int(str(row["id"])): row
        for row in json_object(service.local_section())["local_context_options"]
    }

    assert int(str(rows[4096]["needs_free_mb"])) == 1200
    assert "0.9 GB" in str(rows[4096]["note"]), rows[4096]["note"]
    assert "Qwen2.5-7B" not in str(rows[4096]["note"])


def test_advice_falls_back_to_rules_and_says_so(
    prefs: Preferences, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        local_advice, "read_machine", lambda: Machine("x", 12, 16384, 7000, 500_000, "", 0)
    )
    service = _service(prefs, ask=lambda prompt: (_ for _ in ()).throw(RuntimeError("没配 Key")))

    row = service.recommend_local()

    assert row["source"] == "rule"
    assert "没配 Key" in str(row["note"]), "回落要写明为什么不是 AI 给的"


def test_advice_from_a_model_that_answers_nonsense_falls_back_too(
    prefs: Preferences, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        local_advice, "read_machine", lambda: Machine("x", 12, 16384, 7000, 500_000, "", 0)
    )
    service = _service(prefs, ask=lambda prompt: "你自己看着办")

    row = service.recommend_local()

    assert row["source"] == "rule"
    assert "JSON" in str(row["note"])


def test_applying_advice_writes_the_dials_and_refuses_bad_values(
    prefs: Preferences, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        local_server, "bundled_catalog", lambda: [_row("Qwen3-4B.Q4_K_M.gguf", 2590, 3200)]
    )
    service = _service(prefs)

    problem = service.apply_local_advice(
        {"model": "Qwen3-4B.Q4_K_M.gguf", "context": 8192, "idle_minutes": 5, "eager": False}
    )

    assert problem is None
    assert prefs.text(LOCAL_MODEL, default="") == "Qwen3-4B.Q4_K_M.gguf"
    assert int(prefs.number(LOCAL_CONTEXT, default=0)) == 8192
    assert int(prefs.number(LOCAL_IDLE_MINUTES, default=0)) == 5

    bad = service.apply_local_advice({"context": 3000})
    assert bad and "3000" in bad, "从界面绕回来的值同样要过枚举校验"
    assert int(prefs.number(LOCAL_CONTEXT, default=0)) == 8192, "被拒的不该覆盖掉刚配好的"


def test_advice_without_a_wired_model_still_answers(
    prefs: Preferences, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        local_advice, "read_machine", lambda: Machine("x", 12, 16384, 7000, 500_000, "", 0)
    )
    service = _service(prefs)  # 没 set_advice_ask

    row = service.recommend_local()

    assert row["source"] == "rule"
    assert "没接到模型" in str(row["note"])


# ------------------------------------------------------------------ Qwen3 的思考开关
def pair_like(provider: str, model: str, chosen: bool = True) -> _Pair:
    """ChatService 只看这三格，测试里没必要引它的私有类。"""
    return _Pair(provider=provider, model=model, chosen=chosen)


class _NoopClient:
    def stream(self, messages: Any, *, options: Any = None) -> Iterator[Any]:
        raise AssertionError("这些测试不发请求")

    provider_name = "local"
    model = "qwen3-4b"

    def complete(self, messages: Any, *, options: Any = None) -> Any:
        raise AssertionError("这些测试不发请求")


def test_qwen3_on_the_bundled_server_is_told_to_stop_thinking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(local_server, "selected_card", lambda: _card())

    service = ChatService(lambda: _NoopClient(), session_id="t")
    service.start()

    options = service._thinking_options(None, pair_like("local", "qwen3-4b"))

    assert options.template_kwargs == {"enable_thinking": False}


def test_tools_no_longer_excuse_ignoring_the_thinking_checkbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """r77 exempted tool turns because 1.7B wrote the call as plain text with thinking off.

    Re-measured r90 on 1.7B and on the shipped 4B: the calls come back identically
    either way, and thinking off is about ten times faster (3.4s against 32.2s on the
    4B for 「检查本机 JDK 版本」). The exemption had become the only reason 「不思考」 did
    nothing on the turns where it costs the most.
    """
    monkeypatch.setattr(local_server, "selected_card", lambda: _card())
    from jarvis.llm.types import GenerationOptions

    service = ChatService(lambda: _NoopClient(), session_id="t")
    service.start()

    options = service._thinking_options(
        GenerationOptions(tools=({"function": {"name": "current_time"}},)),
        pair_like("local", "qwen3-4b"),
    )

    assert options.template_kwargs == {"enable_thinking": False}
    assert options.tools, "工具表不能被这一路顺手摘掉"


def test_a_cloud_provider_never_gets_chat_template_kwargs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(local_server, "selected_card", lambda: _card())

    options = ChatService(lambda: _NoopClient(), session_id="t")._thinking_options(
        None, pair_like("deepseek", "deepseek-chat")
    )

    assert options.template_kwargs is None, "别的端点可能不认这个字段，别给它加"


def test_thinking_left_on_is_still_sent_explicitly(monkeypatch: pytest.MonkeyPatch) -> None:
    """On is the template default, so this changes no behaviour -- but it is what the
    checkbox says, and the pair that can carry the field is the pair that gets it."""
    monkeypatch.setattr(local_server, "selected_card", lambda: _card())

    service = ChatService(
        lambda: _NoopClient(),
        session_id="t",
        thinking_provider=lambda: ThinkingRequest(True, 1024),
    )
    service.start()

    options = service._thinking_options(None, pair_like("local", "qwen3-4b"))

    assert options.template_kwargs == {"enable_thinking": True}
    assert options.thinking is None, "顶层那一个是 llama.cpp 不认的字段，别再发"
    assert (
        options.thinking_budget is None
    ), "实测 32/128/512/2048 想出来的字数一样（~850），这个预算在这台服务端上不动任何东西"


# ---------------------------------------------------- 读机器：一次按下去不能等十几秒
def test_reading_the_machine_starts_no_process_when_the_registry_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """注册表给得出就不许再叫 PowerShell —— 那一步实测要 12 秒以上。"""

    def no_process(*args: object, **kwargs: object) -> str:
        raise AssertionError("读机器不该起子进程")

    monkeypatch.setattr(local_advice, "_registry_hardware", lambda: ("i7-10750H", "GTX 1650", 4094))
    monkeypatch.setattr(local_advice, "_powershell", no_process)

    machine = local_advice.read_machine()

    assert machine.cpu == "i7-10750H"
    assert machine.gpu == "GTX 1650"
    assert machine.vram_mb == 4094
    assert "GTX 1650" in machine.facts()


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="注册表只在 Windows 上有")
def test_the_registry_really_names_this_machine() -> None:
    """不是"能跑"，是"跑得出这台机器"：CPU 名要真从注册表里读到。"""
    cpu, _gpu, _vram = local_advice._registry_hardware()

    assert cpu, "注册表读不出 CPU 名，那这条快路是假的"


def test_a_blank_answer_says_she_did_not_answer() -> None:
    """没配 Key 时空回答的下游症状是"找不到 JSON"，那句会把人引偏。"""
    with pytest.raises(ValueError, match="没答上"):
        local_advice.ai_advice(
            Machine("x", 12, 16384, 7000, 500_000, "GTX 1650", 4094),
            [_row("Qwen3-4B.Q4_K_M.gguf", 2590, 3200)],
            lambda prompt: "",
        )


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="注册表只在 Windows 上有")
def test_the_registry_names_a_display_adapter() -> None:
    """每台 Windows 都在那个类键下有一个显示适配器；读不出来就是 GUID 抄错了。

    上一版就是抄成了 ``...080009be1031`` —— 键不存在，异常被吞掉，显卡那格变成
    "不知道"，规则引擎跟着按最保守的一档算，而屏幕上什么都看不出错了。
    """
    _cpu, gpu, _vram = local_advice._registry_hardware()

    assert gpu, f"读不出显示适配器：类键 {local_server.GPU_CLASS_KEY} 大概不存在"


# ------------------------------------------------------------------ 门槛：选中那一刻就查
def test_the_preview_and_the_real_gate_are_one_reading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """下拉框预览用的 `verdict_for` 必须和 `plan()` 得到同一个答案。

    两处各算一遍是这一轮已经付过学费的写法：屏上说"能跑"、点下去起不来，
    或者反过来拦住一个其实跑得动的，都算同一类错。
    """
    # 旋钮里点一个有名字的文件：这样"预览拿当前值"和"预览自己猜一个"两种写法会分出结果。
    want = local_server.Tuning(context=8192, idle_seconds=600.0, model="named.gguf")
    monkeypatch.setattr(local_server, "_TUNING", want)
    monkeypatch.setattr(
        local_server,
        "selected_model",
        lambda wanted: Path(f"{wanted}") if wanted == "named.gguf" else None,
    )
    # 空闲内存是当场读的，两次调用之间本来就会变；钉住它，比的才是"同一段判定"。
    monkeypatch.setattr(local_server, "free_memory_mb", lambda: 7000)

    assert local_server.verdict_for(want.model, want.context, want.eager) == local_server.plan()


def test_a_model_bigger_than_ram_is_refused_even_when_eager(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """「内存看着不够也照样加载」是留给估算太保守的逃生口，不是让人把 45 GB 往 16 GB 上塞。"""
    huge = tmp_path / "huge.gguf"
    huge.write_bytes(b"x" * 4096)
    monkeypatch.setattr(local_server, "selected_model", lambda wanted: huge)
    monkeypatch.setattr(local_server, "total_memory_mb", lambda: 0)
    monkeypatch.setattr(local_server, "free_memory_mb", lambda: 999_999)

    soft = local_server.verdict_for("huge.gguf", 4096, eager=False)
    forced = local_server.verdict_for("huge.gguf", 4096, eager=True)

    assert soft.can_run is False and forced.can_run is False, "eager 不该能越过这条底线"
    assert forced.hard is True
    assert "总内存" in forced.reason and "换一个" in forced.reason
    # 界面拿到的是 preflight 那份：hard 标记必须由后端带出去，不能让前端自己比大小。
    assert local_server.preflight("huge.gguf", 4096)["hard"] is True


def test_preflight_carries_every_number_the_popup_shows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """弹窗上那几个数必须由同一次读凑齐 —— 少一个键，界面就得自己猜。"""
    small = tmp_path / "small.gguf"
    small.write_bytes(b"x" * 4096)
    monkeypatch.setattr(local_server, "selected_model", lambda wanted: small)
    monkeypatch.setattr(local_server, "total_memory_mb", lambda: 16384)
    monkeypatch.setattr(local_server, "free_memory_mb", lambda: 9000)

    verdict = local_server.preflight("small.gguf", 4096)

    assert {
        "ok",
        "reason",
        "free_mb",
        "total_mb",
        "need_mb",
        "file_mb",
        "vram_mb",
        "gpu",
        "disk_free_mb",
        "model_label",
        "hard",
    } <= set(verdict), sorted(verdict)
    assert verdict["ok"] is True and verdict["hard"] is False
    assert verdict["total_mb"] == 16384 and verdict["free_mb"] == 9000


def test_the_machine_profile_and_the_gate_read_the_same_hardware(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """注册表读硬件只留一份：改 llm 层那一份，app 层的机器档案要跟着变。"""
    monkeypatch.setattr(local_server, "_HARDWARE", ("改过的 CPU", "改过的卡", 1234))

    machine = local_advice.read_machine()

    assert machine.cpu == "改过的 CPU"
    assert machine.gpu == "改过的卡"
    assert machine.vram_mb == 1234


def test_undo_returns_to_the_last_model_that_passed_the_gate() -> None:
    """「换回上一个」不许到 form.local_model 里现读上一个值。

    select 的 v-model 先写、模板里的 @change 才跑，所以事件回调里读到的 form.local_model
    已经是要拦的那一个 —— r80 打包版上点出来过：选 11.2 GB 那个文件、点「换回上一个」，
    下拉停在 11.2 GB 上，屏上写着换回了、其实什么都没换。
    """
    panel = (REPO / "frontend/src/components/SettingsPanel.vue").read_text(encoding="utf-8")
    pick = panel[panel.index("async function checkPick(") : panel.index("function gateUndo(")]
    assert "gateWas.value = lastPassed.value" in pick
    for forbidden in ("gateWas.value = current", "gateWas.value = form.value.local_model"):
        assert (
            forbidden not in pick
        ), f"gateWas 又改成现读那一个了（{forbidden}）—— 读到的是要拦的模型"
    assert "else lastPassed.value = model" in pick, "过了门槛要前进，否则下一次拦下来回到更早的那个"
    assert "lastPassed.value = snapshot.local_model" in panel, "打开面板时得先接上存档里那一个"
    force = panel[panel.index("function gateForce(") : panel.index("function gb(")]
    assert "lastPassed.value = form.value.local_model" in force, "人自己点「还是选它」的那条算过了"


def test_applying_the_advice_reads_status_instead_of_sneaking_a_load() -> None:
    """「照这个配」把旋钮写好之后只许**读**状态，不许替人按下「现在加载」。

    ``loadModelServer`` 会真起一个几 GB 的子进程。建议卡自己在屏上写的是「换模型要先停掉
    再现在加载」，点一下「照这个配」就偷跑加载，和它当面矛盾（r86 审计）。
    """
    panel = (REPO / "frontend/src/components/SettingsPanel.vue").read_text(encoding="utf-8")
    start = panel.index("async function useAdvice(")
    body = panel[start : panel.index("\n}\n", start)]
    assert "await readModelServer()" in body, "得让那一格按引擎回读，而不是停在旧读数"
    assert "await loadModelServer(" not in body, "这条路不许起子进程"
    reader = panel[panel.index("async function readModelServer(") :]
    reader = reader[: reader.index("\n}\n")]
    assert "modelServerStatus()" in reader, "「读状态」那条得真的只问状态"


def _local_section() -> Any:
    """一份带「自带模型」那一行的配置，且那一行写的还是 defaults.yaml 里那个旧名字。"""
    from jarvis.config.schema import ModelSpec, ProviderSection

    local_row = ProviderSection(
        name="local",
        base_url="http://127.0.0.1:28181/v1",
        models=(ModelSpec(id="qwen2.5-7b"),),
        default_model="qwen2.5-7b",
        api_key_env="LOCAL_NOTHING",
        cost_input_per_1m=0.0,
        cost_output_per_1m=0.0,
        key_optional=True,
    )
    return _section(providers={**_section().providers, "local": local_row})


def _picker_service(prefs: Preferences) -> SettingsService:
    return SettingsService(prefs, _FakeLlm(), _local_section, environ={})  # type: ignore[arg-type]


def test_the_bundled_row_in_settings_shows_the_picked_model(
    prefs: Preferences, monkeypatch: pytest.MonkeyPatch
) -> None:
    """设置面板里那一行显示的模型 = 「选哪个离线模型」选中的那个，不再是配置里那个旧的。

    一次读：model / models / default_model 三格必须来自同一次 selected_card()，
    否则中途换模型会让同一行同时出现两个名字（r79 定下的那条规矩）。
    """
    service = _picker_service(prefs)

    # 每问一次换一个答案：那一行的三格里只要有两格来自两次读，就会当场对不上。
    rotation = [
        MODEL_CARDS["Qwen3-4B.Q4_K_M.gguf"],
        MODEL_CARDS["Qwen3-0.6B-Q8_0.gguf"],
        MODEL_CARDS["Qwen2.5-7B-Instruct.Q4_K_M.gguf"],
    ]
    asked: list[str] = []

    def card() -> ModelCard:
        asked.append("asked")
        return rotation[min(len(asked) - 1, len(rotation) - 1)]

    monkeypatch.setattr(local_server, "selected_card", card)

    rows = {str(row["name"]): row for row in service.snapshot()["models"]}
    alpha = rows["alpha"]
    shown = rows["local"]

    assert shown["model"] == "qwen3-4b", shown
    assert shown["default_model"] == "qwen3-4b"
    assert [str(m["id"]) for m in shown["models"]] == ["qwen3-4b"]
    assert "qwen2.5-7b" not in str(shown), "配置里那个旧名字不该再出现在这一行"
    assert alpha["model"] == "alpha-1", "跟随只发生在自带那一行，云端的模型名仍是配置里的"


def test_the_bundled_row_refuses_model_edits_and_says_why(prefs: Preferences) -> None:
    """那一行的模型表不许在这下面加删，而且拒绝的话要说清去哪儿改。

    不做端到端的话，用户会在这里加一个模型、点保存、屏幕上什么都没变 —— 一枚点了必然
    失败的按钮，比没有这枚按钮更糟。
    """
    service = _picker_service(prefs)

    added = service.add_model("local", "my-model")
    removed = service.remove_model("local", "qwen2.5-7b")

    assert added["ok"] is False and "选哪个离线模型" in str(added["error"]), added
    assert removed["ok"] is False and "选哪个离线模型" in str(removed["error"]), removed
    stored = prefs.get(LLM_OVERRIDES, default={})
    assert isinstance(stored, dict) and "local" not in stored, f"被拒的编辑不该留下半个：{stored}"


def test_the_bundled_row_is_marked_locked_for_the_page(prefs: Preferences) -> None:
    """锁是哪一行的，由后端告诉页面 —— 前端不许自己抄 'local' 这个名字。"""
    service = _picker_service(prefs)

    rows = {str(row["name"]): row for row in service.snapshot()["models"]}

    assert rows["local"]["models_locked"] is True
    assert rows["alpha"]["models_locked"] is False


def test_the_chat_picker_row_carries_the_lock_too(prefs: Preferences) -> None:
    """顶栏那一路（``model_choices``）也问得到锁，不然页面只能回去抄 'local'。

    上面那条只钉住了设置那一路。2026-10-18 实拍：顶栏写的是"只有一个模型就不显示这一格"，
    而自带那一行永远只有一张卡，于是屏上从头到尾只写着 local —— 加载的是 0.6B 还是 4B
    看不出来，用户只能怀疑软件坏了（三条新提问被答成同一句原话）。
    """
    service = _picker_service(prefs)

    rows = {str(row["name"]): row for row in service.model_choices()["providers"]}

    assert rows["local"]["models_locked"] is True, rows["local"]
    assert rows["alpha"]["models_locked"] is False, rows["alpha"]


def test_the_smoke_verdict_sits_on_the_small_card_only() -> None:
    """判词挂在正确的卡上：四张里只有 0.6B 是"仅冒烟"，买家自己丢进来的不算。

    反向那半同样要钉 —— 四张全 True 和全 False 一样没用，只是把提示变成了噪音。
    """
    flagged = {card.file for card in MODEL_CARDS.values() if card.smoke_only}

    assert flagged == {"Qwen3-0.6B-Q8_0.gguf"}, flagged
    assert local_server.estimate_card(Path("自己下的.gguf")).smoke_only is False


def test_the_settings_catalog_shows_the_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    """设置里那张模型表读的是 catalog：判词没带出去，选的时候仍然只看得到"多大、要约几 GB"。

    0.6B 恰恰是这表里最便宜最快的一档 —— 只报体积不报能力，选错是这张表自己促成的。
    """
    monkeypatch.setattr(local_server, "installed_models", lambda: [])
    rows = {str(row["file"]): row for row in local_server.bundled_catalog()}

    assert rows["Qwen3-0.6B-Q8_0.gguf"]["smoke_only"] is True
    assert rows["Qwen3-4B.Q4_K_M.gguf"]["smoke_only"] is False


def test_a_smoke_only_card_says_so_in_the_name_the_chat_header_shows(
    prefs: Preferences, monkeypatch: pytest.MonkeyPatch
) -> None:
    """判词从卡上走到那一格的名字里：0.6B 要说"仅冒烟"，4B 不许跟着喊。

    两张卡都取自 ``MODEL_CARDS``（不新造一张带这个字段的假卡），所以这条同时钉住
    "判据在哪张卡上"和"这句话真的会出现在屏上"——中间任何一环不读那个字段都会红。
    """
    service = _picker_service(prefs)

    def shown_as(card: ModelCard) -> str:
        monkeypatch.setattr(local_server, "selected_card", lambda: card)
        rows = {str(row["name"]): row for row in service.model_choices()["providers"]}
        return str(rows["local"]["models"][0]["label"])

    small = shown_as(MODEL_CARDS["Qwen3-0.6B-Q8_0.gguf"])
    big = shown_as(MODEL_CARDS["Qwen3-4B.Q4_K_M.gguf"])

    assert small == "自带·Qwen3-0.6B（离线·仅冒烟）", small
    assert big == "自带·Qwen3-4B（离线）", big
