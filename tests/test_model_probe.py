"""测一下：问一句「你真的连得上吗」，以及这个答案存在哪儿。

保存一条模型配置以前是不问的：名字打错一个字母、key 没有那个模型的权限，都要到第一次
提问才暴露，而那时候用户看到的只是"她没答上来"。现在保存这条路必须先连通，并且连通
的时候顺手量一件别人量不到的事 —— 这个模型吃不吃图。看图能力决定了粘贴的截图到底
发不发出去，所以它必须是测出来的，不是清单上写的。

这里的 client 是替身，网络一次也没走。真走一次的那条在
``scripts/probe_vision.py``，需要用户本人的 key。
"""

from __future__ import annotations

import base64
import struct
import zlib
from pathlib import Path
from typing import Any

from jarvis.app.model_probe import ModelCaps, ModelProber, ProbeResult, probe, red_png_data_url
from jarvis.app.preferences import LLM_CAPS, Preferences
from jarvis.llm.errors import LlmAuthError, LlmRequestError
from jarvis.llm.types import ChatMessage, ChatResponse, Usage


class ProbedClient:
    """A client that answers whatever the test scripted for each of the two asks."""

    def __init__(
        self,
        *,
        text: str = "收到",
        picture: str | Exception = "红色",
        provider: str = "testai",
        model: str = "test-model",
    ) -> None:
        self._text = text
        self._picture = picture
        self.provider_name = provider
        self.model = model
        self.asked: list[ChatMessage] = []

    def complete(self, messages: Any, *, options: Any = None) -> ChatResponse:
        message = messages[-1]
        self.asked.append(message)
        if message.images:
            if isinstance(self._picture, Exception):
                raise self._picture
            return ChatResponse(content=self._picture, model=self.model, usage=Usage(30, 2))
        if not self._text:
            return ChatResponse(content="", model=self.model)
        return ChatResponse(content=self._text, model=self.model, usage=Usage(9, 2))

    def stream(self, messages: Any, *, options: Any = None) -> Any:
        return iter(())


class RefusingClient(ProbedClient):
    """A client whose every request raises what the provider would have."""

    def __init__(self, error: Exception) -> None:
        super().__init__()
        self._error = error

    def complete(self, messages: Any, *, options: Any = None) -> ChatResponse:
        raise self._error


def _caps(tmp_path: Path) -> ModelCaps:
    return ModelCaps(Preferences(tmp_path / "prefs.json"))


def _verdict(answer: dict[str, object]) -> tuple[dict[str, object], dict[str, object]]:
    """``(problems, applied)`` out of a settings answer, narrowed once."""
    problems = answer["problems"]
    applied = answer["applied"]
    assert isinstance(problems, dict) and isinstance(applied, dict)
    return problems, applied


class TestThePictureItself:
    def test_the_probe_picture_is_a_real_red_png(self) -> None:
        """The claim under every vision answer: this is a red square a model can see.

        Checked by parsing what the builder emits rather than trusting it -- a corrupt or
        non-red PNG would make every 支持看图 below a measurement of nothing.
        """
        data = red_png_data_url()
        assert data.startswith("data:image/png;base64,")
        raw = base64.b64decode(data.split(",", 1)[1])
        assert raw[:8] == b"\x89PNG\r\n\x1a\n"
        width, height, depth, color = struct.unpack(">IIBB", raw[16:26])
        assert (width, height, depth, color) == (64, 64, 8, 2)
        assert zlib.crc32(raw[12:29]) == struct.unpack(">I", raw[29:33])[0]

        # The pixels themselves. A red square is the whole premise of the vision probe:
        # if the file decoded to something else, every 「支持看图」 in this file would be a
        # measurement of nothing at all.
        body = zlib.decompress(raw[41 : 41 + struct.unpack(">I", raw[33:37])[0]])
        stride = 1 + width * 3
        assert len(body) == stride * height
        rows = [body[row * stride : row * stride + stride] for row in range(height)]
        assert all(row[0] == 0 for row in rows), "每行的滤镜字节必须是 0（不过滤）"
        assert all(row[1:] == b"\xff\x00\x00" * width for row in rows), "整张图必须全是红"


class TestTheProbe:
    def test_an_answer_is_enough_to_save(self) -> None:
        client = ProbedClient()
        result = probe(client)
        assert result.ok is True
        assert result.vision is True
        assert result.provider == "testai"
        assert result.model == "test-model"
        assert len(client.asked) == 2

    def test_a_key_that_does_not_work_is_an_answer_not_a_crash(self) -> None:
        client = RefusingClient(LlmAuthError("API key environment variable 'X' is not set"))
        result = probe(client)
        assert result.ok is False
        assert "API key" in result.detail
        assert result.vision is None, "连文字都答不上，谈不上看图"

    def test_a_model_that_answers_nothing_is_not_a_pass(self) -> None:
        """ "Connected" has to mean something came back.

        An endpoint that returns 200 and an empty message is a broken configuration, and
        saving it quietly would leave the operator with a blank bubble and no reason.
        """
        assert probe(ProbedClient(text="")).ok is False

    def test_a_provider_that_refuses_the_picture_is_reported_as_blind_not_broken(self) -> None:
        client = ProbedClient(picture=LlmRequestError("this model does not accept images"))
        result = probe(client)
        assert result.ok is True, "连得上文字，这个模型不是坏了"
        assert result.vision is False, "拒了图就是看不见，别记成支持"

    def test_a_model_that_takes_the_picture_but_describes_the_wrong_colour_is_blind(
        self,
    ) -> None:
        """Both halves have to hold.

        A model that accepts the request and then says 蓝色 has not seen anything; filing
        that as 支持 would send the operator's screenshots to a model that guesses.
        """
        assert probe(ProbedClient(picture="这是一张蓝色的图")).vision is False

    def test_the_second_question_can_be_skipped(self) -> None:
        client = ProbedClient()
        result = probe(client, with_vision=False)
        assert result.ok is True
        assert result.vision is None
        assert len(client.asked) == 1


class TestTheRecord:
    def test_a_passing_probe_makes_the_pair_saveable(self, tmp_path: Path) -> None:
        caps = _caps(tmp_path)
        assert caps.verified("testai", "test-model") is False
        caps.record(
            "testai", "test-model", ProbeResult(True, "收到", True, 12.0, "testai", "test-model")
        )
        assert caps.verified("testai", "test-model") is True
        assert caps.vision("testai", "test-model") is True

    def test_a_failing_probe_records_nothing_worth_having(self, tmp_path: Path) -> None:
        caps = _caps(tmp_path)
        caps.record("testai", "bad", ProbeResult(False, "连不上", None, 1.0, "testai", "bad"))
        assert caps.verified("testai", "bad") is False

    def test_a_measurement_expires(self, tmp_path: Path) -> None:
        """A provider can add eyes, and can also retire a model. Thirty days is a guess.

        What is not a guess is that a row from last quarter must not keep promising a
        capability nobody has seen since.
        """
        caps = _caps(tmp_path)
        caps.record("testai", "old", ProbeResult(True, "收到", True, 1.0, "testai", "old"))
        stored = caps._table()
        for row in stored.values():
            row["at"] = "2020-01-01T00:00:00"
        caps._prefs.set(LLM_CAPS, stored)
        assert caps.verified("testai", "old") is False
        assert caps.vision("testai", "old") is None

    def test_the_answer_survives_the_file(self, tmp_path: Path) -> None:
        """Written by one process, read by the next one after a restart.

        A capability that only lived in memory would make every boot start out blind, and
        the panel would show 「未测」 for models that were tested last week.
        """
        path = tmp_path / "prefs.json"
        first = ModelCaps(Preferences(path))
        first.record(
            "deepseek",
            "vision-probe",
            ProbeResult(True, "收到", False, 3.0, "deepseek", "vision-probe"),
        )
        reopened = ModelCaps(Preferences(path))
        reopened._prefs._values = None
        assert reopened.verified("deepseek", "vision-probe") is True
        assert reopened.vision("deepseek", "vision-probe") is False

    def test_rows_come_back_split_apart_for_the_page(self, tmp_path: Path) -> None:
        caps = _caps(tmp_path)
        caps.record("qwen", "qwen-vl", ProbeResult(True, "收到", True, 2.0, "qwen", "qwen-vl"))
        rows = caps.as_rows()
        assert [(row["provider"], row["model"]) for row in rows] == [("qwen", "qwen-vl")]
        assert "\x00" not in str(rows)

    def test_deleting_a_model_takes_its_measurement_with_it(self, tmp_path: Path) -> None:
        """Otherwise the next model named the same inherits last model's eyes."""
        caps = _caps(tmp_path)
        caps.record("qwen", "gone", ProbeResult(True, "收到", True, 2.0, "qwen", "gone"))
        caps.forget("qwen", "gone")
        assert caps.verified("qwen", "gone") is False
        assert caps.vision("qwen", "gone") is None


class TestThePanelDoors:
    """The two bridge methods the 设置 panel presses, and the rule behind them."""

    def test_a_model_that_does_not_answer_is_taken_back_out_again(self, tmp_path: Any) -> None:
        from tests.test_ui_desktop import _bridge

        calls: list[tuple[str, str]] = []

        class Settings:
            def add_model(self, provider: str, model_id: str, label: str = "") -> dict[str, object]:
                calls.append(("add", f"{provider}/{model_id}"))
                return {"ok": True, "models": [model_id]}

            def remove_model(self, provider: str, model_id: str = "") -> dict[str, object]:
                calls.append(("remove", f"{provider}/{model_id}"))
                return {"ok": True, "models": []}

        prober = ModelProber(lambda p, m: ProbedClient(text=""), _caps(tmp_path))
        bridge = _bridge(tmp_path, settings=Settings(), model_probe=prober)
        answer = bridge.llm_add_model("testai", "typo-model")

        assert answer["ok"] is False
        assert "没保存" in str(answer["error"])
        assert ("remove", "testai/typo-model") in calls, "存下了又删掉，才叫没保存"

    def test_a_model_that_answers_stays(self, tmp_path: Any) -> None:
        from tests.test_ui_desktop import _bridge

        class Settings:
            def add_model(self, provider: str, model_id: str, label: str = "") -> dict[str, object]:
                return {"ok": True, "models": [model_id]}

            def remove_model(self, provider: str, model_id: str = "") -> dict[str, object]:
                raise AssertionError("连上了就不该回滚")

        prober = ModelProber(lambda p, m: ProbedClient(), _caps(tmp_path))
        bridge = _bridge(tmp_path, settings=Settings(), model_probe=prober)
        answer = bridge.llm_add_model("testai", "test-model")

        assert answer["ok"] is True
        assert answer["probe"]["vision"] is True  # type: ignore[index]

    def test_a_probe_that_cannot_even_be_built_reports_the_reason(self, tmp_path: Any) -> None:
        from jarvis.core.exceptions import ConfigurationError
        from tests.test_ui_desktop import _bridge

        def client_for(provider: str, model: str) -> Any:
            raise ConfigurationError(f"unknown LLM provider: '{provider}'")

        bridge = _bridge(
            tmp_path,
            settings=None,
            model_probe=ModelProber(client_for, _caps(tmp_path)),
        )
        verdict = bridge.llm_test("nope", "m")
        assert verdict["ok"] is False
        assert "unknown LLM provider" in str(verdict["detail"])

    def test_a_bulk_add_probes_each_row_and_keeps_the_ones_that_answer(self, tmp_path: Any) -> None:
        """The panel can queue several providers; the gate has to walk the queue.

        Skipping the list would quietly drop the connectivity rule for exactly the path
        where it matters most -- three endpoints typed in one sitting, one of them a typo.
        """
        from tests.test_ui_desktop import _bridge

        removed: list[str] = []

        class Settings:
            def apply(self, patch: dict[str, Any], **_: Any) -> dict[str, object]:
                if "remove_model" in patch:
                    removed.append(str(patch["remove_model"]))
                return {"ok": True, "applied": dict(patch), "problems": {}, "models": []}

            def thinking_loader(self) -> str:
                return "dots"

            def speaks_typed(self) -> bool:
                return False

        def client_for(provider: str, model: str) -> ProbedClient:
            return ProbedClient(text="") if provider == "deadai" else ProbedClient()

        bridge = _bridge(
            tmp_path,
            settings=Settings(),
            model_probe=ModelProber(client_for, _caps(tmp_path)),
        )

        answer = bridge.settings_apply(
            {
                "add_model": [
                    {"name": "goodai", "base_url": "https://g.example/v1", "model": "m-1"},
                    {"name": "deadai", "base_url": "https://d.example/v1", "model": "m-2"},
                ]
            }
        )

        assert removed == ["deadai"], "只有连不上的那行该被拿回去"
        problems, applied = _verdict(answer)
        assert "deadai" in str(problems["add_model"])
        # The row that answered must not be in the complaint: naming both means the
        # operator goes and re-types the one that was fine.
        assert "goodai" not in str(problems["add_model"])
        assert "add_model" not in applied, "一批里有一行没成，就不能说整批存好了"

    def test_a_bulk_add_where_everything_answers_keeps_every_row(self, tmp_path: Any) -> None:
        from tests.test_ui_desktop import _bridge

        calls: list[Any] = []

        class Settings:
            def apply(self, patch: dict[str, Any], **_: Any) -> dict[str, object]:
                calls.append(patch)
                return {"ok": True, "applied": dict(patch), "problems": {}, "models": []}

            def thinking_loader(self) -> str:
                return "dots"

            def speaks_typed(self) -> bool:
                return False

        bridge = _bridge(
            tmp_path,
            settings=Settings(),
            model_probe=ModelProber(lambda p, m: ProbedClient(), _caps(tmp_path)),
        )

        answer = bridge.settings_apply(
            {
                "add_model": [
                    {"name": "oneai", "base_url": "https://1.example/v1", "model": "m"},
                    {"name": "twoai", "base_url": "https://2.example/v1", "model": "m"},
                ]
            }
        )

        assert [patch for patch in calls if "remove_model" in patch] == []
        problems, applied = _verdict(answer)
        assert problems == {}
        assert "add_model" in applied

    def test_no_probe_wired_says_so_instead_of_passing_quietly(self, tmp_path: Any) -> None:
        """A build without the door must not look like a build where everything passed."""
        from tests.test_ui_desktop import _bridge

        bridge = _bridge(tmp_path, settings=None)
        assert bridge.llm_test("a", "b")["ok"] is False


def test_the_prober_files_what_it_learned(tmp_path: Path) -> None:
    """The finding has to outlive the button press.

    A probe nobody recorded is a log line: the operator saw it pass once, and the next
    question is still answered as if the model had never been tested.
    """
    caps = _caps(tmp_path)
    prober = ModelProber(lambda p, m: ProbedClient(), caps)
    assert prober.test("testai", "test-model")["ok"] is True
    assert caps.verified("testai", "test-model") is True
    assert caps.vision("testai", "test-model") is True
