"""``search_web`` and the text extraction ``fetch_url`` now does before answering.

Both halves exist because of one measurement: a fetched page reached the model as 4000
characters of ``<div class=...>``, so on a 4K local window the article itself never
made it into the prompt at all. Search is the other half -- 「最新的」 questions have no
answer on this machine, and pretending otherwise is what produced a run of 「资料库里没
有」 answers to a request to read the web.

Nothing here touches the network: ``opener=`` is the seam the module already exposed.
"""

from __future__ import annotations

import urllib.error
import urllib.parse
from typing import Any

import pytest

from jarvis.core.exceptions import ToolExecutionError
from jarvis.tools.builtins import web_tools


class _Response:
    def __init__(self, body: bytes, content_type: str = "text/html", url: str = "") -> None:
        self._body = body
        self._ct = content_type
        self._url = url

    def read(self, _limit: int = -1) -> bytes:
        return self._body

    def geturl(self) -> str:
        return self._url

    @property
    def headers(self) -> Any:
        outer = self

        class _H:
            def get_content_type(self) -> str:
                return outer._ct

        return _H()

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_a: object) -> None:
        """``None`` so an exception inside the block still propagates."""


class _Opener:
    """The smallest thing that satisfies ``opener.open(request, timeout=...)``."""

    def __init__(self, response: _Response | None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.requests: list[Any] = []

    def open(self, request: Any, timeout: float = 0) -> _Response:
        del timeout
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        assert self.response is not None
        return self.response


def _handler(index: int = 1, *, opener: Any = None, timeout: float = 5.0) -> Any:
    return web_tools.build(timeout_seconds=timeout, opener=opener)[index][1]


def _rows(count: int, title: str = "T", body: str = "s") -> str:
    return "".join(
        f'<li class="b_algo"><h2><a href="u">{title}{n}</a></h2>'
        f'<p class="b_lineclamp2">{body}{n}</p></li>'
        for n in range(count)
    )


_BING_PAGE = (
    "<html><body><ol id='b_results'>"
    '<li class="b_algo"><h2><a href="https://www.bing.com/ck/a?p=abc">'
    "Qwen3: <b>Think Deeper</b></a></h2>"
    "<cite>https://qwen.ai › blog</cite>"
    '<p class="b_lineclamp2">2025年4月28日 · 支持 119 种语言。</p></li>'
    '<li class="b_algo"><h2><a href="https://www.bing.com/ck/a?p=def">'
    "GitHub - QwenLM/Qwen3</a></h2>"
    "<cite>https://github.com/QwenLM</cite>"
    '<p class="b_lineclamp2">The Qwen3 series.</p></li>'
    '<li class="b_algo"><h2><a href="x">   </a></h2></li>'
    "</ol></body></html>"
)


class TestSearchWeb:
    def test_results_carry_title_source_and_snippet(self) -> None:
        opener = _Opener(_Response(_BING_PAGE.encode(), url=web_tools.SEARCH_ENDPOINT))
        out = _handler(1, opener=opener)({"query": "Qwen3", "max_results": 3})
        assert "Qwen3: Think Deeper" in out
        assert "https://qwen.ai/blog" in out, "「 › 」要还原成能读的路径"
        assert "支持 119 种语言" in out
        assert "搜索引擎：bing" in out

    def test_a_blank_title_row_is_skipped_not_emitted_as_empty(self) -> None:
        opener = _Opener(_Response(_BING_PAGE.encode(), url=web_tools.SEARCH_ENDPOINT))
        out = _handler(1, opener=opener)({"query": "Qwen3"})
        assert out.count("[") == 2, "第三行没有标题，不该占一个编号"

    def test_the_query_is_encoded_into_a_fixed_host(self) -> None:
        """The model supplies text, never a URL -- so it cannot aim the request.

        A query that *looks* like an address must end up percent-encoded inside ``q=``,
        not as the host. That is the whole SSRF argument for this tool's shape.
        """
        opener = _Opener(_Response(b"<html></html>", url=web_tools.SEARCH_ENDPOINT))
        _handler(1, opener=opener)({"query": "https://127.0.0.1:8/admin"})
        sent = opener.requests[0].full_url
        parsed = urllib.parse.urlsplit(sent)
        origin = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        assert origin == web_tools.SEARCH_ENDPOINT
        # The private address survives only as opaque, percent-encoded query text.
        assert "127.0.0.1" not in origin
        assert urllib.parse.parse_qs(parsed.query)["q"] == ["https://127.0.0.1:8/admin"]
        assert "%3A%2F%2F" in sent

    def test_result_count_is_capped(self) -> None:
        opener = _Opener(_Response(_rows(20).encode(), url=web_tools.SEARCH_ENDPOINT))
        out = _handler(1, opener=opener)({"query": "x", "max_results": 5})
        assert out.count("[") == web_tools.MAX_RESULTS
        assert "T19" not in out

    def test_an_oversized_max_results_is_clamped_not_refused(self) -> None:
        opener = _Opener(_Response(_rows(20).encode(), url=web_tools.SEARCH_ENDPOINT))
        out = _handler(1, opener=opener)({"query": "x", "max_results": 999})
        assert out.count("[") == web_tools.MAX_RESULTS

    def test_an_empty_result_page_says_so_instead_of_inventing_rows(self) -> None:
        """The hard requirement: no fabricated search results, ever."""
        opener = _Opener(_Response(b"<html><body>nothing</body></html>", url="https://x/y"))
        out = _handler(1, opener=opener)({"query": "完全不存在的词"})
        assert "没有搜到结果" in out
        assert "fetch_url" in out, "要给出下一步能做什么，而不是只报失败"

    def test_a_network_failure_reports_the_real_reason(self) -> None:
        opener = _Opener(None, error=urllib.error.URLError("Connection refused"))
        with pytest.raises(ToolExecutionError) as exc:
            _handler(1, opener=opener)({"query": "天气"})
        assert "Connection refused" in str(exc.value)

    def test_an_empty_query_is_refused_before_any_request(self) -> None:
        opener = _Opener(_Response(b"", url="https://x"))
        for blank in ("", "   ", None):
            with pytest.raises(ToolExecutionError):
                _handler(1, opener=opener)({"query": blank})
        assert opener.requests == []

    def test_output_is_capped_at_the_documented_budget(self) -> None:
        page = _rows(5, title="标" * 200, body="文" * 900).encode()
        opener = _Opener(_Response(page, url="https://x/y"))
        out = _handler(1, opener=opener)({"query": "x"})
        assert len(out) <= web_tools.MAX_RESULT_CHARS

    def test_it_declares_itself_a_network_tool(self) -> None:
        spec = web_tools.build()[1][0]
        assert spec.name == "search_web"
        assert spec.permissions == frozenset({"network"})


class TestFetchUrlTextExtraction:
    def test_html_becomes_prose_not_tags(self) -> None:
        page = (
            "<html><body><script>var tracking=1;</script>"
            "<h1>标题</h1><p>正文一段</p></body></html>"
        ).encode()
        opener = _Opener(_Response(page, "text/html", url="https://example.com/a"))
        out = _handler(0, opener=opener)({"url": "https://example.com/a"})
        assert "正文一段" in out
        for noise in ("<div", "<p>", "tracking", "<h1>"):
            assert noise not in out

    def test_json_is_passed_through_untouched(self) -> None:
        body = b'{"ok": true, "value": 42}'
        opener = _Opener(_Response(body, "application/json", url="https://example.com/api"))
        out = _handler(0, opener=opener)({"url": "https://example.com/api"})
        assert '"value": 42' in out

    def test_entities_are_unescaped(self) -> None:
        opener = _Opener(
            _Response(b"<p>A &amp; B &#8212; C</p>", "text/html", url="https://example.com")
        )
        out = _handler(0, opener=opener)({"url": "https://example.com"})
        assert "A & B" in out and "—" in out

    def test_a_page_with_no_body_says_so(self) -> None:
        opener = _Opener(_Response(b"<html><body></body></html>", "text/html", url="https://x/y"))
        out = _handler(0, opener=opener)({"url": "https://x/y"})
        assert "没有可读正文" in out

    def test_max_chars_still_bounds_the_answer(self) -> None:
        page = f"<p>{'字' * 5000}</p>".encode()
        opener = _Opener(_Response(page, "text/html", url="https://example.com"))
        out = _handler(0, opener=opener)({"url": "https://example.com", "max_chars": 120})
        assert out.count("字") <= 120

    def test_loopback_is_still_refused(self) -> None:
        opener = _Opener(_Response(b"", url="http://127.0.0.1:28181/v1/models"))
        with pytest.raises(ToolExecutionError):
            _handler(0, opener=opener)({"url": "http://127.0.0.1:28181/v1/models"})
        assert opener.requests == []

    def test_a_redirect_into_the_private_range_is_refused_afterwards(self) -> None:
        opener = _Opener(_Response(b"<p>hi</p>", "text/html", url="http://localhost/secret"))
        with pytest.raises(ToolExecutionError):
            _handler(0, opener=opener)({"url": "https://example.com/jumps"})

    def test_the_old_raw_html_behaviour_would_have_swallowed_the_budget(self) -> None:
        """Regression in the other direction: tags must not crowd out the article.

        Before extraction a 4000-character budget held roughly 400 characters of prose
        and 3600 of markup, which is the whole reason 「联网」 looked broken offline.
        """
        markup = '<div class="wrapper"><span id="x">' * 400
        page = (markup + "<p>真正的一句话</p>").encode()
        opener = _Opener(_Response(page, "text/html", url="https://example.com"))
        out = _handler(0, opener=opener)({"url": "https://example.com", "max_chars": 4000})
        assert "真正的一句话" in out


class TestValidateUrlStillStrict:
    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "example.com/no-scheme",
            "ftp://example.com/file",
            "file:///C:/Windows/win.ini",
            "http://localhost:8080",
            "http://192.168.1.1/admin",
            "http://10.0.0.5/",
            "http://169.254.169.254/latest/meta-data/",
        ],
    )
    def test_refused(self, bad: str) -> None:
        with pytest.raises(ToolExecutionError):
            web_tools.validate_url(bad)

    @pytest.mark.parametrize(
        "ok", ["https://example.com/a", "http://example.com:8080/a?b=1"], ids=("https", "port")
    )
    def test_accepted(self, ok: str) -> None:
        assert web_tools.validate_url(ok).startswith("http")


class _RoutingOpener:
    """按 URL 分发响应，并记下试的先后 —— 兜底链的价值全在顺序上。

    一个响应喂四家的话，测出来的是"最前面那家能不能解析"，而不是"前面几家哑了后面
    还接不接得上"，而后者才是这条链存在的理由。
    """

    def __init__(self, pages: dict[str, _Response], *, fail: tuple[str, ...] = ()) -> None:
        self.pages = pages
        self.fail = fail
        self.attempted: list[str] = []

    def open(self, request: Any, timeout: float = 0) -> _Response:
        del timeout
        url = request.full_url
        self.attempted.append(url)
        if any(needle in url for needle in self.fail):
            raise urllib.error.URLError("网关把这一发挡了")
        for needle, response in self.pages.items():
            if needle in url:
                return response
        return _Response(b"<html><body>nothing</body></html>", url=url)


_EMPTY = _Response(b"<html><body>no results here</body></html>", url="https://www.bing.com/search")

_RSS_PAGE = _Response(
    (
        '<rss version="2.0"><channel><item>'
        "<title>Qwen3 官方博客</title>"
        "<link>https://qwen.ai/blog</link>"
        "<description>支持 119 种语言</description>"
        "</item></channel></rss>"
    ).encode(),
    content_type="application/rss+xml",
    url="https://www.bing.com/search",
)

_SOGOU_PAGE = _Response(
    (
        '<html><div class="vrwrap"><h3 class="vr-title">'
        '<a href="/link?url=abc123" target="_blank">Qwen3 <em>发布</em></a></h3>'
        '<div class="str-text-info">2025年4月28日发布，支持119种语言。</div></div></html>'
    ).encode(),
    url="https://www.sogou.com/web",
)

_DDG_PAGE = _Response(
    (
        b'<html><div class="result__body">'
        b'<a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?'
        b'uddg=https%3A%2F%2Fexample.org%2Fdoc&amp;rut=beef">Qwen3 docs</a>'
        b"</div></html>"
    ),
    url="https://html.duckduckgo.com/html/",
)


class TestTheKeylessFallbackChain:
    """bing RSS → bing 网页 → 搜狗 → DuckDuckGo：顺序、解包、以及"全哑了要说"。"""

    def test_rss_wins_and_nothing_else_is_asked(self) -> None:
        opener = _RoutingOpener({"format=rss": _RSS_PAGE})
        out = _handler(1, opener=opener)({"query": "Qwen3"})
        assert "搜索引擎：bing" in out
        assert "https://qwen.ai/blog" in out
        assert len(opener.attempted) == 1, "第一家就出结果还挨个问一遍，等于每次搜索打四发"

    def test_bing_empty_falls_through_to_sogou(self) -> None:
        opener = _RoutingOpener({"sogou.com": _SOGOU_PAGE})
        out = _handler(1, opener=opener)({"query": "Qwen3"})
        assert "搜索引擎：sogou" in out
        assert "https://www.sogou.com/link?url=abc123" in out, "搜狗给的是站内跳转，得补成绝对地址"
        assert "2025年4月28日发布" in out
        assert any("sogou.com" in url for url in opener.attempted)

    def test_duckduckgo_unwraps_its_own_redirect(self) -> None:
        opener = _RoutingOpener({"duckduckgo.com": _DDG_PAGE})
        out = _handler(1, opener=opener)({"query": "Qwen3"})
        assert "搜索引擎：duckduckgo" in out
        assert "https://example.org/doc" in out, "uddg 没解包的话，引用到模型那儿的是跳转壳不是来源"
        assert "uddg=" not in out

    def test_every_engine_answering_empty_says_so_without_raising(self) -> None:
        opener = _RoutingOpener({})
        out = _handler(1, opener=opener)({"query": "Qwen3"})
        assert "没有搜到结果" in out and "已尝试免Key搜索备用入口" in out
        assert len(opener.attempted) == 4, "四家都得试到，少一家就是那条路静默没了"

    def test_every_engine_blocked_is_reported_not_called_empty(self) -> None:
        """全被挡 ≠ 全没结果：前者要说"暂不可用"，不然排查方向整个反了。"""
        opener = _RoutingOpener({}, fail=("bing.com", "sogou.com", "duckduckgo.com"))
        with pytest.raises(ToolExecutionError) as error:
            _handler(1, opener=opener)({"query": "Qwen3"})
        assert "免Key搜索暂不可用" in str(error.value)

    def test_a_country_page_is_not_evidence_for_a_university_question(self) -> None:
        """搜索引擎会吞掉半截中文词，回一张国家概况页 —— 那不能当"查到了大学排名"。"""
        generic = _Response(
            (
                b'<rss version="2.0"><channel><item><title>China overview</title>'
                b"<link>https://example.org/china</link>"
                b"<description>Population and area of China.</description></item></channel></rss>"
            ),
            content_type="application/rss+xml",
            url="https://www.bing.com/search",
        )
        ranked = _Response(
            (
                '<html><h3 class="vr-title"><a href="/link?url=x">2025 中国大学排行榜100强</a></h3>'
                "<div>北京大学 清华大学</div></html>"
            ).encode(),
            url="https://www.sogou.com/web",
        )
        opener = _RoutingOpener({"format=rss": generic, "sogou.com": ranked})
        out = _handler(1, opener=opener)({"query": "美国前十的大学有哪些"})
        assert "搜索引擎：sogou" in out, "RSS 那张国家页被当证据的话，模型就照着一页废话排名了"
        assert "大学排行榜" in out
