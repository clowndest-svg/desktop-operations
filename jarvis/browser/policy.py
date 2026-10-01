"""Domain allow/block policy for the browser service.

The policy is the single gate deciding whether a URL may be visited, and it runs
*before* the engine is started, so a blocked navigation never launches a browser
and never touches the network.

Matching rules, kept simple enough to be obviously correct:

* comparison is case-insensitive and ignores a trailing dot;
* a rule ``example.com`` matches the host itself and any subdomain, so both
  ``www.example.com`` and ``api.example.com`` are covered;
* ``block`` is evaluated first — a host on both lists is refused;
* an empty ``allow`` list means "no allow-list", which is only sane together
  with a non-empty ``block`` list (see ``BrowserSection.allow_domains``);
* a URL with no hostname (``file:///…``, ``about:blank``, ``javascript:…``) is
  refused, because there is nothing to match and those schemes reach local
  resources the allow-list was meant to fence off.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from urllib.parse import urlsplit

from jarvis.core.exceptions import BrowserError

logger = logging.getLogger("jarvis.browser.policy")


def _url_hostname(url: str) -> str:
    """Host of a navigable URL, or ``''`` for a scheme with no authority."""
    text = url.strip()
    if "://" not in text:
        if ":" in text:  # about:blank / javascript: / mailto: carry no host
            return ""
        text = f"//{text}"  # tolerate a bare host typed by the user
    return (urlsplit(text).hostname or "").rstrip(".").lower()


def _rule_hostname(rule: str) -> str:
    """Host of an allow/block rule; accepts a bare domain or a full URL."""
    text = rule.strip()
    if "://" not in text:
        text = f"//{text}"
    return (urlsplit(text).hostname or "").rstrip(".").lower()


def _matches(host: str, rule: str) -> bool:
    """True when ``host`` is ``rule`` itself or a subdomain of it."""
    return host == rule or host.endswith(f".{rule}")


class DomainPolicy:
    """Allow/block gate consulted before any navigation."""

    def __init__(self, *, allow: Sequence[str], block: Sequence[str]) -> None:
        # Normalise once at construction so ``check`` stays a cheap comparison
        # and a malformed rule cannot make a later navigation behave oddly.
        self._allow = tuple(host for host in map(_rule_hostname, allow) if host)
        self._block = tuple(host for host in map(_rule_hostname, block) if host)

    @property
    def allow(self) -> tuple[str, ...]:
        """The normalised allow-list (empty means "no allow-list")."""
        return self._allow

    @property
    def block(self) -> tuple[str, ...]:
        """The normalised block-list."""
        return self._block

    def check(self, url: str) -> None:
        """Raise :class:`BrowserError` unless ``url`` may be visited."""
        host = _url_hostname(url)
        if not host:
            raise BrowserError(
                "无法从该地址解析出域名，出于安全考虑已拒绝访问",
                details={"url": url},
            )
        for rule in self._block:
            if _matches(host, rule):
                raise BrowserError(
                    f"域名 {host} 在禁止访问名单中",
                    details={"url": url, "host": host, "rule": rule, "reason": "blocked"},
                )
        if self._allow and not any(_matches(host, rule) for rule in self._allow):
            raise BrowserError(
                f"域名 {host} 不在允许访问名单中",
                details={"url": url, "host": host, "reason": "not_allowed"},
            )
