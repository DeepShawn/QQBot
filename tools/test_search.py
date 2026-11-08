#!/usr/bin/env python3
"""search.py 自测脚本（纯标准库，不需要第三方依赖）。

两部分：
1. 离线：本地起一个 HTTP 假服务，喂假 DuckDuckGo / Bing 页面，断言解析、去重、
   HTML 实体反转义、广告/站内链接过滤、uddg 还原、相对链接、失败回退、编码等；
   完全不依赖外网。
2. 联网：跑 3 个真实查询，打印前 3 条结果，并对链接做 HEAD 检查（非 200 不算
   错误，只统计成功率）。

运行：
    python3 tools/test_search.py              # 离线断言 + 真实联网查询
    python3 tools/test_search.py --offline    # 只跑离线断言
"""

from __future__ import annotations

import base64
import http.server
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BOT_DIR = ROOT / "bot"
sys.path.insert(0, str(BOT_DIR))

import search as search_mod  # noqa: E402

REAL_QUERIES = ["DeepSeek 最新模型", "北京天气", "Python 3.13 新特性"]

PASSED = 0
FAILED: list[str] = []
REQUESTS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASSED
    if ok:
        PASSED += 1
        print(f"  [PASS] {name}")
    else:
        FAILED.append(name + (f" -> {detail}" if detail else ""))
        print(f"  [FAIL] {name}" + (f" -> {detail}" if detail else ""))


# --------------------------------------------------------------------------
# 假页面：DDG html 版 / Bing / 挑战页
# --------------------------------------------------------------------------
_FAKE_DDG = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>fake ddg</title></head>
<body><div id="links" class="results">

<div class="result results_links results_links_deep web-result">
  <div class="links_main links_deep result__body">
    <h2 class="result__title">
      <a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Falpha%3Fa%3D1%26b%3D2&amp;rut=xyz">Alpha &amp; Beta &quot;搜索&quot; &#39;测试&#39;</a>
    </h2>
    <a class="result__snippet" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Falpha%3Fa%3D1%26b%3D2&amp;rut=xyz">第一行 <b>加粗</b> 摘要 &amp; 实体<br>第二行</a>
  </div>
</div>

<div class="result result--ad results_links web-result">
  <div class="links_main result__body">
    <h2 class="result__title"><a rel="nofollow" class="result__a" href="//duckduckgo.com/y.js?ad_domain=ads.example.com&amp;ad_provider=bing">赞助广告 请忽略</a></h2>
    <a class="result__snippet" href="//duckduckgo.com/y.js?ad_domain=ads.example.com">广告摘要</a>
  </div>
</div>

<div class="result results_links results_links_deep web-result">
  <div class="links_main links_deep result__body">
    <h2 class="result__title"><a rel="nofollow" class="result__a" href="https://duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Falpha%3Fa%3D1%26b%3D2">重复结果（应去重）</a></h2>
    <a class="result__snippet" href="#">重复摘要</a>
  </div>
</div>

<div class="result results_links results_links_deep web-result">
  <div class="links_main links_deep result__body">
    <h2 class="result__title"><a rel="nofollow" class="result__a" href="/l/?uddg=https%3A%2F%2Fzh.example.org%2F%E4%B8%AD%E6%96%87">中文标题 &amp; 实体</a></h2>
  </div>
</div>

<div class="result results_links results_links_deep web-result">
  <div class="links_main links_deep result__body">
    <h2 class="result__title"><a rel="nofollow" class="result__a" href="/l/?uddg=https%3A%2F%2Frelative.example.org%2Fpage">相对链接结果</a></h2>
    <a class="result__snippet" href="#">相对链接摘要</a>
  </div>
</div>

<div class="result results_links results_links_deep web-result">
  <div class="links_main links_deep result__body">
    <h2 class="result__title"><a rel="nofollow" class="result__a" href="https://direct.example.net/page">直接链接结果</a></h2>
    <a class="result__snippet" href="#">直接 <b>高亮</b> 摘要</a>
  </div>
</div>

</div></body></html>
"""

_BING_REDIRECT_TARGET = "https://redirect.example.org/page"
_BING_TOKEN = "a1" + base64.urlsafe_b64encode(
    _BING_REDIRECT_TARGET.encode("utf-8")
).decode("ascii").rstrip("=")

_FAKE_BING = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>fake bing</title></head>
<body><ol id="b_results">
<li class="b_algo"><h2><a href="https://news.example.com/a?x=1&amp;y=2">Bing 结果一 <strong>Python</strong> 教程</a></h2><div class="b_caption"><p class="b_lineclamp2">摘要一 &amp; 更多 &lt;标签&gt;</p></div></li>
<li class="b_algo b_ad"><h2><a href="https://ads.example.com/promo">广告结果 请忽略</a></h2><div class="b_caption"><p>广告摘要</p></div></li>
<li class="b_algo"><h2><a href="https://news.example.com/a?x=1&amp;y=2">重复的 Bing 结果</a></h2><div class="b_caption"><p>重复摘要</p></div></li>
<li class="b_algo"><h2><a href="/relative/path">相对链接结果</a></h2><div class="b_caption"><p>相对摘要</p></div></li>
<li class="b_algo"><h2><a href="https://www.bing.com/ck/a?!&amp;&amp;p=abc&amp;u={_BING_TOKEN}&amp;ntb=1">Bing 跳转链接</a></h2><p>跳转摘要</p></li>
</ol></body></html>
"""

_FAKE_CHALLENGE = (
    "<!DOCTYPE html><html><body><h1>Are you a robot?</h1>"
    "<div class='result'>challenge page, no results</div></body></html>"
)


# --------------------------------------------------------------------------
# 本地假搜索引擎
# --------------------------------------------------------------------------
class FakeEngine(http.server.BaseHTTPRequestHandler):
    server_version = "FakeSearch/1.0"

    def _send(self, code: int, body: str, head_only: bool = False) -> None:
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if not head_only:
            self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802 - http.server 约定
        path = urllib.parse.urlsplit(self.path).path
        REQUESTS.append(self.path)
        if path == "/ddg":
            self._send(200, _FAKE_DDG)
        elif path == "/bing":
            self._send(200, _FAKE_BING)
        elif path == "/ddg-challenge":
            self._send(200, _FAKE_CHALLENGE)
        elif path == "/ddg-500":
            self._send(500, "ddg gone")
        elif path == "/bing-500":
            self._send(500, "bing gone")
        else:
            self._send(404, "not found")

    def do_HEAD(self) -> None:  # noqa: N802 - http.server 约定
        path = urllib.parse.urlsplit(self.path).path
        if path == "/head-ok":
            self._send(200, "", head_only=True)
        else:
            self._send(404, "", head_only=True)

    def log_message(self, *args) -> None:  # 静音访问日志
        pass


def start_fake_server() -> http.server.ThreadingHTTPServer:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeEngine)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


# --------------------------------------------------------------------------
# 第一部分：离线断言
# --------------------------------------------------------------------------
def run_offline_tests(port: int) -> None:
    base = f"http://127.0.0.1:{port}"
    original_ddg, original_bing = search_mod.DDG_HTML, search_mod.BING_SEARCH
    try:
        check(
            "import 到的是 bot/search.py",
            Path(search_mod.__file__).resolve() == (BOT_DIR / "search.py").resolve(),
            str(search_mod.__file__),
        )
        search_mod.DDG_HTML = base + "/ddg"
        search_mod.BING_SEARCH = base + "/bing"
        REQUESTS.clear()

        # ---------- DDG ----------
        result = search_mod.search("中文 查询", max_results=5)
        check("DDG ok=True", result["ok"] is True, result.get("error"))
        check("DDG provider=duckduckgo", result["provider"] == "duckduckgo")
        check("DDG error 为空串", result["error"] == "")
        check("DDG query 回显", result["query"] == "中文 查询")

        urls = [item["url"] for item in result["results"]]
        titles = [item["title"] for item in result["results"]]
        snippets = [item["snippet"] for item in result["results"]]

        check(
            "DDG 结果数量=4（去重+过滤后）",
            len(urls) == 4,
            f"实际 {len(urls)}: {urls}",
        )
        check(
            "DDG 标题 HTML 实体反转义",
            result["results"][0]["title"] == 'Alpha & Beta "搜索" \'测试\'',
            repr(result["results"][0]["title"]),
        )
        check(
            "DDG 摘要去标签+压缩空白",
            result["results"][0]["snippet"] == "第一行 加粗 摘要 & 实体 第二行",
            repr(result["results"][0]["snippet"]),
        )
        check(
            "DDG uddg 解码为真实 URL",
            urls[0] == "https://example.com/alpha?a=1&b=2",
            urls[0],
        )
        check(
            "DDG 重复链接去重",
            urls.count("https://example.com/alpha?a=1&b=2") == 1,
            str(urls),
        )
        check(
            "DDG 广告过滤",
            all("ads.example" not in url for url in urls)
            and not any("广告" in title for title in titles),
            str(titles),
        )
        check(
            "DDG 站内链接过滤",
            all("duckduckgo.com" not in url for url in urls),
            str(urls),
        )
        check(
            "DDG 相对链接解析",
            "https://relative.example.org/page" in urls,
            str(urls),
        )
        check(
            "DDG 直接链接保留",
            "https://direct.example.net/page" in urls,
            str(urls),
        )
        check(
            "DDG 中文 URL 百分号编码",
            "https://zh.example.org/%E4%B8%AD%E6%96%87" in urls,
            str(urls),
        )
        check(
            "DDG 查询词用 quote 编码",
            any("q=%E4%B8%AD%E6%96%87%20%E6%9F%A5%E8%AF%A2" in path for path in REQUESTS),
            str(REQUESTS),
        )
        check(
            "DDG 无摘要条目 snippet 为空串",
            any(url.endswith("/%E4%B8%AD%E6%96%87") for url in urls)
            and "" in snippets,
            str(snippets),
        )

        # ---------- 参数容错 ----------
        result2 = search_mod.search("x", max_results="2", timeout="3")
        check(
            "max_results/timeout 字符串参数容错",
            result2["ok"] is True and len(result2["results"]) == 2,
            str(result2),
        )

        # ---------- format_results ----------
        text = search_mod.format_results(result)
        check("format_results 非空且从 1. 开始", text.startswith("1. "), text[:40])
        check(
            "format_results 含标题/链接/摘要",
            "https://example.com/alpha?a=1&b=2" in text
            and "第一行 加粗 摘要 & 实体 第二行" in text,
            text[:200],
        )
        check(
            "format_results 链接行缩进 3 空格",
            "\n   https://example.com/alpha?a=1&b=2" in text,
            text[:200],
        )
        check(
            "format_results 多条编号",
            "2. " in text and "3. " in text and "4. " in text,
            text,
        )
        check(
            "format_results max_chars 限制",
            len(search_mod.format_results(result, 60)) <= 60,
            str(len(search_mod.format_results(result, 60))),
        )
        check(
            "format_results 空结果返回空串",
            search_mod.format_results({"ok": True, "results": []}) == "",
        )
        check(
            "format_results ok=False 返回空串",
            search_mod.format_results({"ok": False, "error": "x", "results": []}) == "",
        )
        check("format_results None 返回空串", search_mod.format_results(None) == "")

        # ---------- Bing ----------
        result3 = search_mod.search("bing 测试", provider="bing")
        burls = [item["url"] for item in result3["results"]]
        btitles = [item["title"] for item in result3["results"]]
        bsnippets = [item["snippet"] for item in result3["results"]]
        check("Bing ok=True", result3["ok"] is True, result3.get("error"))
        check("Bing provider=bing", result3["provider"] == "bing")
        check("Bing 结果数量=3（去重+过滤后）", len(burls) == 3, str(burls))
        check(
            "Bing 广告过滤",
            all("ads.example" not in url for url in burls)
            and not any("广告" in title for title in btitles),
            str(btitles),
        )
        check(
            "Bing 重复链接去重",
            burls.count("https://news.example.com/a?x=1&y=2") == 1,
            str(burls),
        )
        check("Bing 相对链接解析", base + "/relative/path" in burls, str(burls))
        check(
            "Bing /ck/a 站内跳转解码",
            _BING_REDIRECT_TARGET in burls,
            str(burls),
        )
        check(
            "Bing 摘要实体反转义",
            "摘要一 & 更多 <标签>" in bsnippets,
            str(bsnippets),
        )
        check(
            "Bing 标题去标签",
            "Bing 结果一 Python 教程" in btitles,
            str(btitles),
        )

        # ---------- auto 回退 ----------
        search_mod.DDG_HTML = base + "/ddg-challenge"
        result4 = search_mod.search("fallback", provider="auto")
        check(
            "auto：DDG 无结果时回退 Bing",
            result4["ok"] is True and result4["provider"] == "bing",
            str(result4),
        )

        # ---------- 全失败 ----------
        search_mod.DDG_HTML = base + "/ddg-500"
        search_mod.BING_SEARCH = base + "/bing-500"
        result5 = search_mod.search("全部失败", provider="auto")
        check(
            "auto：全失败 ok=False 且不抛异常",
            result5["ok"] is False and result5["results"] == [],
            str(result5),
        )
        check(
            "auto：error 含两家失败原因",
            "duckduckgo" in result5["error"] and "bing" in result5["error"],
            result5["error"],
        )

        # ---------- 非法入参 ----------
        search_mod.DDG_HTML = base + "/ddg"
        search_mod.BING_SEARCH = base + "/bing"
        result6 = search_mod.search("x", provider="google")
        check(
            "未知 provider 返回 ok=False",
            result6["ok"] is False and "google" in result6["error"],
            str(result6),
        )
        result7 = search_mod.search("   ")
        check(
            "空查询返回 ok=False 且 results 为空",
            result7["ok"] is False and result7["results"] == [],
            str(result7),
        )

        search_mod.BING_SEARCH = base + "/bing-500"
        result8 = search_mod.search("x", provider="bing")
        check(
            "单个 provider 失败不抛异常",
            result8["ok"] is False and "bing" in result8["error"],
            str(result8),
        )
    finally:
        search_mod.DDG_HTML, search_mod.BING_SEARCH = original_ddg, original_bing


# --------------------------------------------------------------------------
# 第二部分：真实联网查询
# --------------------------------------------------------------------------
def head_ok(url: str, timeout: float = 8.0) -> tuple[bool, str]:
    request = urllib.request.Request(
        url, method="HEAD", headers={"User-Agent": search_mod.USER_AGENT}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status == 200, f"HTTP {response.status}"
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}"
    except Exception as exc:  # noqa: BLE001 - 只做统计
        return False, type(exc).__name__


def run_network_tests() -> None:
    print("\n=== 第二部分：真实联网查询（DDG → Bing 自动回退）===")
    queries_ok = 0
    links_total = 0
    links_ok = 0
    for query in REAL_QUERIES:
        print(f"\n查询：{query}")
        started = time.time()
        result = search_mod.search(query, max_results=5)
        elapsed = time.time() - started
        if not result["ok"] or not result["results"]:
            print(f"  ✗ 失败（{elapsed:.1f}s）：{result.get('error')}")
            continue
        queries_ok += 1
        print(
            f"  ✓ ok  provider={result['provider']}  "
            f"共 {len(result['results'])} 条  耗时 {elapsed:.1f}s"
        )
        for index, item in enumerate(result["results"][:3], 1):
            print(f"  {index}. {item['title'][:70]}")
            print(f"     {item['url']}")
            if item["snippet"]:
                print(f"     {item['snippet'][:70]}")
            reachable, note = head_ok(item["url"])
            links_total += 1
            links_ok += 1 if reachable else 0
            print(f"     HEAD: {'200' if reachable else note}")
        preview = search_mod.format_results(result, 400)
        print("  format_results 预览：")
        for line in preview.splitlines()[:8]:
            print("    " + line)

    rate = (100.0 * links_ok / links_total) if links_total else 0.0
    print(
        f"\n真实查询成功 {queries_ok}/{len(REAL_QUERIES)}；"
        f"链接 HEAD 200 成功率 {links_ok}/{links_total}（{rate:.0f}%）"
    )
    if queries_ok == 0:
        print("提示：真实查询全部失败，请检查网络/VPN；离线断言部分仍然有效。")


def main() -> int:
    only_offline = "--offline" in sys.argv or "--no-network" in sys.argv
    server = start_fake_server()
    port = server.server_address[1]
    print("=== 第一部分：本地假页面离线断言 ===")
    try:
        run_offline_tests(port)
    finally:
        server.shutdown()
        server.server_close()

    total = PASSED + len(FAILED)
    print(f"\n离线断言：{PASSED}/{total} 通过")
    if FAILED:
        print("失败项：")
        for item in FAILED:
            print("  - " + item)

    if only_offline:
        print("已跳过真实联网查询（--offline）。")
    else:
        run_network_tests()
    return 0 if not FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
