"""联网搜索模块（纯标准库，无第三方依赖）。

优先 DuckDuckGo html 版（https://html.duckduckgo.com/html/），失败自动回退
Bing（https://cn.bing.com/search）。对外只暴露两个函数：

    search(query, max_results=5, provider="auto", timeout=12.0) -> dict
        {"ok": bool, "query": str, "provider": str,
         "results": [{"title": str, "url": str, "snippet": str}], "error": str}

    format_results(result, max_chars=1800) -> str
        把 search() 的结果格式化成给大模型看的纯文本；无结果返回空字符串。

设计约束：
- 只用 urllib / html / re / urllib.parse 等标准库；
- 网络或解析失败不抛异常，一律通过 ok=False + error 表达；
- 结果 url 保证是可点击的完整 http(s) 链接（DDG 的 /l/?uddg= 会解码）；
- 结果按 URL 去重，过滤广告与搜索引擎站内链接。

用法示例::

    from search import search, format_results
    res = search("北京天气", max_results=5)
    if res["ok"]:
        print(format_results(res))
"""

from __future__ import annotations

import base64
import html
import math
import re
import time
import urllib.parse
import urllib.request

__all__ = ["search", "format_results"]

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# 测试脚本会临时改写这两个地址，指向本地假页面，因此不要写死进函数默认参数。
DDG_HTML = "https://html.duckduckgo.com/html/"
BING_SEARCH = "https://cn.bing.com/search"

DEFAULT_TIMEOUT = 12.0
_PROVIDERS = ("duckduckgo", "bing")
_MAX_RESULTS = 20
_MAX_PARSE = 50  # 单页最多解析多少条原始结果，再去重截断

# 搜索引擎站内链接 / 广告跳转域名，命中即丢弃
_ENGINE_HOSTS = ("duckduckgo.com", "bing.com", "go.microsoft.com")

_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.S | re.I)
_BR_RE = re.compile(r"<br\s*/?>", re.I)
_WS_RE = re.compile(r"\s+")
_HREF_RE = re.compile(r"""\bhref\s*=\s*(["'])(.*?)\1""", re.S | re.I)
_ANCHOR_RE = re.compile(r"<a\b([^>]*)>(.*?)</a>", re.S | re.I)

_DDG_A_RE = re.compile(
    r"""<a\b([^>]*\bclass\s*=\s*["'][^"']*\bresult__a\b[^"']*["'][^>]*)>(.*?)</a>""",
    re.S | re.I,
)
_DDG_SNIPPET_RE = re.compile(
    r"""<(?:a|div|span|p)\b[^>]*\bclass\s*=\s*["'][^"']*\bresult__snippet\b[^"']*["'][^>]*>(.*?)</(?:a|div|span|p)>""",
    re.S | re.I,
)
_DDG_RESULT_DIV_RE = re.compile(
    r"""<div\b[^>]*\bclass\s*=\s*["'][^"']*\bresult\b[^"']*["'][^>]*>""", re.I
)

_BING_LI_RE = re.compile(
    r"""<li\b[^>]*\bclass\s*=\s*["']([^"']*\bb_algo\b[^"']*)["'][^>]*>""", re.I
)
_BING_H2_RE = re.compile(r"<h2\b[^>]*>(.*?)</h2>", re.S | re.I)
_BING_P_RE = re.compile(r"<p\b[^>]*>(.*?)</p>", re.S | re.I)
_BING_CAPTION_RE = re.compile(
    r"""<div\b[^>]*\bclass\s*=\s*["'][^"']*\bb_caption\b[^"']*["'][^>]*>(.*?)</div>""",
    re.S | re.I,
)
_BING_AD_CLASS_RE = re.compile(r"\bb_ad\b", re.I)


class SearchError(Exception):
    """搜索过程中可预期的失败（网络、风控、解析为空）；不会抛给调用方。"""


# --------------------------------------------------------------------------
# HTTP / 文本小工具
# --------------------------------------------------------------------------
def _build_url(base: str, query: str) -> str:
    """拼搜索地址，中文等用 urllib.parse.quote 编码。"""
    base = (base or "").strip().rstrip("?&")
    return base + "?q=" + urllib.parse.quote(str(query), safe="")


def _decode(raw: bytes, charset: str | None = None) -> str:
    """按 HTTP 头 / meta 声明 / UTF-8 / GB18030 依次尝试解码。"""
    candidates: list[str] = []
    if charset:
        candidates.append(charset)
    m = re.search(
        rb"""<meta[^>]+charset\s*=\s*["']?\s*([a-zA-Z0-9_\-]+)""", raw[:4096], re.I
    )
    if m:
        candidates.append(m.group(1).decode("ascii", "ignore"))
    candidates.extend(["utf-8", "gb18030"])
    for enc in candidates:
        try:
            return raw.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", "replace")


def _http_get(url: str, timeout: float) -> str:
    """GET 一个页面并返回文本；失败时抛异常（由 search() 统一兜住）。"""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Cache-Control": "no-cache",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        charset = resp.headers.get_content_charset()
    return _decode(raw, charset)


def _clean_text(fragment: str | None) -> str:
    """去标签、反转义 HTML 实体、压缩空白，得到一行纯文本。"""
    if not fragment:
        return ""
    text = _SCRIPT_RE.sub(" ", fragment)
    text = _BR_RE.sub(" ", text)
    text = _TAG_RE.sub("", text)
    text = html.unescape(text)
    return _WS_RE.sub(" ", text).strip()


def _clean_attr(value: str | None) -> str:
    return html.unescape(value or "").strip()


def _host(url: str) -> str:
    try:
        return (urllib.parse.urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def _is_usable_url(url: str) -> bool:
    """只接受带主机名的绝对 http(s) 链接。"""
    if not url or not isinstance(url, str):
        return False
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    if parts.scheme not in ("http", "https"):
        return False
    host = (parts.hostname or "").lower()
    if not host:
        return False
    if host != "localhost" and "." not in host:
        return False
    return True


def _url_to_ascii(url: str) -> str:
    """把链接里的中文等非 ASCII 字符转成百分号编码（保留已有 %XX 与 URL 保留字符）。"""
    try:
        return urllib.parse.quote(url, safe=":/?#[]@!$&'()*+,;=%")
    except (TypeError, ValueError):
        return url


def _url_key(url: str) -> str:
    """去重键：忽略协议、大小写主机名、fragment 和末尾斜杠。"""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return ""
    host = (parts.hostname or "").lower()
    if not host:
        return ""
    path = parts.path.rstrip("/") or "/"
    query = parts.query
    return host + path + (("?" + query) if query else "")


def _is_engine_url(url: str) -> bool:
    """站内链接 / 广告跳转，直接丢弃。"""
    host = _host(url)
    for engine in _ENGINE_HOSTS:
        if host == engine or host.endswith("." + engine):
            return True
    return False


def _looks_like_ad(url: str) -> bool:
    if "duckduckgo.com/y.js" in url:
        return True
    try:
        query = urllib.parse.urlsplit(url).query
    except ValueError:
        return True
    params = urllib.parse.parse_qs(query)
    return any(key in params for key in ("ad_domain", "ad_provider", "ad_type"))


# --------------------------------------------------------------------------
# 链接还原
# --------------------------------------------------------------------------
def _unwrap_ddg(href: str) -> str:
    """把 duckduckgo.com/l/?uddg=... 还原成真实地址，并处理相对链接。"""
    href = _clean_attr(href)
    if not href:
        return ""
    if href.startswith("//"):
        href = "https:" + href
    try:
        parts = urllib.parse.urlsplit(href)
    except ValueError:
        return ""
    params = urllib.parse.parse_qs(parts.query)
    target = (params.get("uddg") or [""])[0].strip()
    if target:
        return target
    if parts.scheme in ("http", "https"):
        return href
    return urllib.parse.urljoin("https://duckduckgo.com/", href)


def _unwrap_bing(href: str) -> str:
    """处理 Bing 的相对链接、站内跳转（/ck/a?u=a1...）；站内链接返回空串。"""
    href = _clean_attr(href)
    if not href:
        return ""
    if href.startswith("//"):
        href = "https:" + href
    try:
        url = urllib.parse.urljoin(BING_SEARCH, href)
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return ""
    host = (parts.hostname or "").lower()
    if host == "bing.com" or host.endswith(".bing.com"):
        target = ""
        if parts.path.startswith("/ck/a"):
            params = urllib.parse.parse_qs(parts.query)
            token = (params.get("u") or [""])[0]
            if token.startswith("a1"):
                token = token[2:]
            token += "=" * (-len(token) % 4)
            try:
                target = base64.urlsafe_b64decode(token).decode("utf-8", "replace")
            except (ValueError, TypeError):
                target = ""
        if target.startswith(("http://", "https://")):
            return target
        return ""  # 搜索引擎站内页，不是结果链接
    return url


# --------------------------------------------------------------------------
# HTML 解析
# --------------------------------------------------------------------------
def _ddg_anchor_is_ad(page: str, anchor_start: int) -> bool:
    """从锚点往前找最近的 result 容器，看它是否带 result--ad 广告标记。"""
    last = None
    for match in _DDG_RESULT_DIV_RE.finditer(page, 0, anchor_start):
        last = match
    if last is None:
        return False
    return "result--ad" in page[last.start() : last.end()]


def _parse_ddg(page: str) -> list[dict]:
    """解析 DuckDuckGo html 版：result__a 标题 + result__snippet 摘要。"""
    anchors = list(_DDG_A_RE.finditer(page))
    results: list[dict] = []
    for index, match in enumerate(anchors):
        href_match = _HREF_RE.search(match.group(1))
        if not href_match:
            continue
        if _ddg_anchor_is_ad(page, match.start()):
            continue
        url = _unwrap_ddg(href_match.group(2))
        title = _clean_text(match.group(2))
        # 摘要就在当前锚点到下一个结果锚点之间
        end = anchors[index + 1].start() if index + 1 < len(anchors) else len(page)
        tail = page[match.end() : end]
        snippet_match = _DDG_SNIPPET_RE.search(tail)
        snippet = _clean_text(snippet_match.group(1)) if snippet_match else ""
        if not _is_usable_url(url) or _is_engine_url(url) or _looks_like_ad(url):
            continue
        results.append({"title": title or url, "url": url, "snippet": snippet})
        if len(results) >= _MAX_PARSE:
            break
    return results


def _parse_bing(page: str) -> list[dict]:
    """解析 Bing：li.b_algo 里的 h2 a 标题 + 首个 <p> 摘要。"""
    starts = list(_BING_LI_RE.finditer(page))
    results: list[dict] = []
    for index, match in enumerate(starts):
        classes = match.group(1) or ""
        if _BING_AD_CLASS_RE.search(classes):  # b_ad 广告位
            continue
        end = starts[index + 1].start() if index + 1 < len(starts) else len(page)
        body = page[match.end() : end]
        h2 = _BING_H2_RE.search(body)
        scope = h2.group(1) if h2 else body
        anchor = _ANCHOR_RE.search(scope)
        if not anchor and h2:
            anchor = _ANCHOR_RE.search(body)
        if not anchor:
            continue
        href_match = _HREF_RE.search(anchor.group(1))
        if not href_match:
            continue
        url = _unwrap_bing(href_match.group(2))
        title = _clean_text(anchor.group(2))
        snippet = ""
        for paragraph in _BING_P_RE.finditer(body):
            candidate = _clean_text(paragraph.group(1))
            if candidate:
                snippet = candidate
                break
        if not snippet:
            caption = _BING_CAPTION_RE.search(body)
            if caption:
                snippet = _clean_text(caption.group(1))
        if not _is_usable_url(url) or _is_engine_url(url) or _looks_like_ad(url):
            continue
        results.append({"title": title or url, "url": url, "snippet": snippet})
        if len(results) >= _MAX_PARSE:
            break
    return results


def _dedupe(results: list[dict]) -> list[dict]:
    """按 URL 去重，过滤站内/广告链接。"""
    seen: set[str] = set()
    unique: list[dict] = []
    for item in results:
        url = _url_to_ascii(str(item.get("url") or "").strip())
        if not _is_usable_url(url) or _is_engine_url(url) or _looks_like_ad(url):
            continue
        key = _url_key(url)
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(
            {
                "title": str(item.get("title") or url),
                "url": url,
                "snippet": str(item.get("snippet") or ""),
            }
        )
    return unique


# --------------------------------------------------------------------------
# 各 provider 的搜索实现
# --------------------------------------------------------------------------
def _search_duckduckgo(query: str, max_results: int, timeout: float) -> list[dict]:
    page = _http_get(_build_url(DDG_HTML, query), timeout)
    results = _dedupe(_parse_ddg(page))
    if not results:
        raise SearchError("duckduckgo 未解析到结果（可能被风控或页面结构变化）")
    return results[:max_results]


def _search_bing(query: str, max_results: int, timeout: float) -> list[dict]:
    page = _http_get(_build_url(BING_SEARCH, query), timeout)
    results = _dedupe(_parse_bing(page))
    if not results:
        raise SearchError("bing 未解析到结果（可能被风控或页面结构变化）")
    return results[:max_results]


def _err_text(exc: Exception) -> str:
    if isinstance(exc, SearchError):
        text = str(exc)
    else:
        text = f"{type(exc).__name__}: {exc}"
    return _WS_RE.sub(" ", text).strip()[:180]


def _normalize_provider(provider) -> str:
    name = str(provider or "auto").strip().lower()
    aliases = {
        "ddg": "duckduckgo",
        "duckduckgo.com": "duckduckgo",
        "html.duckduckgo.com": "duckduckgo",
        "bing.com": "bing",
        "cn.bing.com": "bing",
        "": "auto",
    }
    return aliases.get(name, name)


# --------------------------------------------------------------------------
# 对外接口
# --------------------------------------------------------------------------
def search(
    query: str,
    max_results: int = 5,
    provider: str = "auto",
    timeout: float = 12.0,
) -> dict:
    """联网搜索，返回统一结构，不抛异常。

    Args:
        query: 搜索词（可含中文）。
        max_results: 最多返回条数（1~20，默认 5）。
        provider: "duckduckgo" / "bing" / "auto"（默认，依次尝试 DDG → Bing）。
        timeout: 整体超时预算秒数（默认 12；auto 时两个引擎共享，
            单次请求至少 1 秒，因此 auto 最坏总耗时约为 timeout + 1）。

    Returns:
        {"ok": bool, "query": str, "provider": str,
         "results": [{"title": str, "url": str, "snippet": str}],
         "error": str}
        成功时 provider 为实际使用的引擎、error 为空串；
        失败时 ok=False、results 为空、error 为可读原因。
    """
    query_text = "" if query is None else str(query).strip()
    provider_name = _normalize_provider(provider)
    outcome: dict = {
        "ok": False,
        "query": query_text,
        "provider": provider_name,
        "results": [],
        "error": "",
    }

    if not query_text:
        outcome["error"] = "查询内容为空"
        return outcome

    if provider_name != "auto" and provider_name not in _PROVIDERS:
        outcome["error"] = (
            f"不支持的 provider：{provider_name}（可选 duckduckgo / bing / auto）"
        )
        return outcome

    try:
        limit = int(max_results)
    except (TypeError, ValueError):
        limit = 5
    limit = max(1, min(_MAX_RESULTS, limit))

    try:
        seconds = float(timeout)
    except (TypeError, ValueError):
        seconds = DEFAULT_TIMEOUT
    if not math.isfinite(seconds) or seconds <= 0:
        seconds = DEFAULT_TIMEOUT
    seconds = max(1.0, min(30.0, seconds))

    # timeout 是整体预算：auto 依次尝试两个引擎时共享，避免最坏耗时翻倍。
    deadline = time.monotonic() + seconds
    engines = _PROVIDERS if provider_name == "auto" else (provider_name,)
    errors: list[str] = []
    for engine in engines:
        searcher = _search_duckduckgo if engine == "duckduckgo" else _search_bing
        attempt_timeout = max(1.0, deadline - time.monotonic())
        try:
            results = searcher(query_text, limit, attempt_timeout)
        except Exception as exc:  # noqa: BLE001 - 对外约定失败不抛异常
            errors.append(f"{engine}: {_err_text(exc)}")
            continue
        if not results:
            errors.append(f"{engine}: 未解析到结果")
            continue
        outcome["ok"] = True
        outcome["provider"] = engine
        outcome["results"] = results
        outcome["error"] = ""
        return outcome

    outcome["error"] = "；".join(errors) or "搜索失败"
    return outcome


def format_results(result: dict, max_chars: int = 1800) -> str:
    """把 search() 结果格式化成给大模型看的纯文本，无结果返回空串。

    格式（每条之间空一行）：:

        1. 标题
           https://...
           摘要…

    总长度不超过 max_chars；超长时只保留能完整放下的条目并加省略号。
    """
    if not isinstance(result, dict) or not result.get("ok"):
        return ""
    items = result.get("results")
    if not isinstance(items, list) or not items:
        return ""

    try:
        limit = int(max_chars)
    except (TypeError, ValueError):
        limit = 1800
    if limit <= 0:
        return ""

    blocks: list[str] = []
    number = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        title = _WS_RE.sub(" ", str(item.get("title") or "")).strip()
        url = str(item.get("url") or "").strip()
        snippet = _WS_RE.sub(" ", str(item.get("snippet") or "")).strip()
        if not title and not url:
            continue
        number += 1
        lines = [f"{number}. {title or url}"]
        if url:
            lines.append("   " + url)
        if snippet:
            lines.append("   " + snippet)
        blocks.append("\n".join(lines))

    if not blocks:
        return ""

    text = ""
    truncated = False
    for block in blocks:
        candidate = block if not text else "\n\n" + block
        if len(text) + len(candidate) <= limit:
            text += candidate
        else:
            truncated = True
            break

    if not text:
        # 第一条就超长：硬截断
        return blocks[0][:limit]
    if truncated:
        suffix = "\n…"
        text = text + suffix if len(text) + len(suffix) <= limit else text[:limit]
    return text
