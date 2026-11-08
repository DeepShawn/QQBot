#!/usr/bin/env python3
"""media.py 自测：本地起 HTTP 服务，现场生成测试文件，全部用断言。

覆盖：
- GBK 中文 txt（utf-8 → gbk → latin-1 解码链）
- 最小 OOXML 现场构造：docx / pptx / xlsx → 抽中文
- pypdf 真生成极简 PDF（没装 pypdf 时验证 error 分支）
- 超过 max_bytes → MediaError
- 非 http(s) URL → MediaError
- allowed_hosts 白名单 / 子域匹配 / 重定向绕过
- 截断、图片、未知类型、损坏 docx 的容错

运行：python3 tools/test_media.py
"""
from __future__ import annotations

import io
import logging
import sys
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parent.parent
BOT_DIR = ROOT / "bot"
sys.dont_write_bytecode = True  # 不在 bot/ 下生成 __pycache__
sys.path.insert(0, str(BOT_DIR))

import media  # noqa: E402

DOCX_CTYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PPTX_CTYPE = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
XLSX_CTYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
P_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
S_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"

GBK_TEXT = "你好，世界！这是 GBK 编码的测试文本。\n第二行：文件处理模块。"
UTF8_TEXT = "UTF-8 中文：机器人收到文件后会自动抽取文本。\n第二行。"


# ---------------------------------------------------------------- 现场构造文件


def make_zip(parts: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, payload in parts.items():
            zf.writestr(name, payload)
    return buf.getvalue()


def _content_types_xml(overrides: dict[str, str]) -> bytes:
    body = "".join(
        '<Override PartName="%s" ContentType="%s"/>' % (part, ctype)
        for part, ctype in overrides.items()
    )
    xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        f"{body}</Types>"
    )
    return xml.encode("utf-8")


def make_docx(paragraphs: list[str]) -> bytes:
    body = "".join(
        '<w:p><w:r><w:t xml:space="preserve">%s</w:t></w:r></w:p>' % escape(p)
        for p in paragraphs
    )
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document xmlns:w="{W_NS}"><w:body>{body}</w:body></w:document>'
    )
    return make_zip(
        {
            "[Content_Types].xml": _content_types_xml(
                {"/word/document.xml": "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"}
            ),
            "word/document.xml": document.encode("utf-8"),
        }
    )


def make_pptx(slides: list[list[str]]) -> bytes:
    parts: dict[str, bytes] = {}
    for index, lines in enumerate(slides, 1):
        paragraphs = "".join(
            "<a:p><a:r><a:t>%s</a:t></a:r></a:p>" % escape(line) for line in lines
        )
        slide = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<p:sld xmlns:a="{A_NS}" xmlns:p="{P_NS}">'
            "<p:cSld><p:spTree><p:sp><p:txBody>"
            f"{paragraphs}"
            "</p:txBody></p:sp></p:spTree></p:cSld></p:sld>"
        )
        parts[f"ppt/slides/slide{index}.xml"] = slide.encode("utf-8")
    parts["[Content_Types].xml"] = _content_types_xml(
        {"/ppt/slides/slide1.xml": "application/vnd.openxmlformats-officedocument.presentationml.slide+xml"}
    )
    return make_zip(parts)


def _col_letter(index: int) -> str:
    letters = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def make_xlsx(rows: list[list[object]]) -> bytes:
    shared: list[str] = []
    shared_index: dict[str, int] = {}

    def sid(value: str) -> int:
        if value not in shared_index:
            shared_index[value] = len(shared)
            shared.append(value)
        return shared_index[value]

    row_xml: list[str] = []
    for row_no, row in enumerate(rows, 1):
        cells: list[str] = []
        for col_no, value in enumerate(row):
            ref = f"{_col_letter(col_no)}{row_no}"
            if isinstance(value, (int, float)):
                cells.append(f'<c r="{ref}"><v>{value}</v></c>')
            else:
                cells.append(f'<c r="{ref}" t="s"><v>{sid(str(value))}</v></c>')
        row_xml.append(f'<row r="{row_no}">{"".join(cells)}</row>')

    shared_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<sst xmlns="{S_NS}">'
        + "".join("<si><t>%s</t></si>" % escape(s) for s in shared)
        + "</sst>"
    )
    sheet_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<worksheet xmlns="{S_NS}"><sheetData>{"".join(row_xml)}</sheetData></worksheet>'
    )
    return make_zip(
        {
            "[Content_Types].xml": _content_types_xml(
                {
                    "/xl/sharedStrings.xml": "application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml",
                    "/xl/worksheets/sheet1.xml": "application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml",
                }
            ),
            "xl/sharedStrings.xml": shared_xml.encode("utf-8"),
            "xl/worksheets/sheet1.xml": sheet_xml.encode("utf-8"),
        }
    )


def make_pdf(text: str = "Hello PDF 12345") -> bytes:
    """手写一个 pypdf 能解析的一页 PDF（Helvetica，纯 ASCII 文本）。"""
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(("BT /F1 24 Tf 72 720 Td (%s) Tj ET" % text).encode("latin-1"))
        + ("BT /F1 24 Tf 72 720 Td (%s) Tj ET" % text).encode("latin-1")
        + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, obj in enumerate(objects, 1):
        offsets.append(len(out))
        out += ("%d 0 obj\n" % number).encode() + obj + b"\nendobj\n"
    xref_offset = len(out)
    out += ("xref\n0 %d\n" % (len(objects) + 1)).encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += ("%010d 00000 n \n" % offset).encode()
    out += (
        "trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n"
        % (len(objects) + 1, xref_offset)
    ).encode()
    return bytes(out)


GBK_BYTES = GBK_TEXT.encode("gbk")
UTF8_BYTES = UTF8_TEXT.encode("utf-8")
DOCX_BYTES = make_docx(
    ["第一章 中文段落", "这是 docx 里的第二段文字。", "特殊字符 & <tag> 测试"]
)
PPTX_BYTES = make_pptx([["第一页：项目介绍"], ["第二页：进度 100%"]])
XLSX_BYTES = make_xlsx([["姓名", "数量"], ["苹果", "12"], ["香蕉", "7"]])
PDF_BYTES = make_pdf()
BIG_BYTES = b"x" * 300_000

ROUTES: dict[str, tuple[str, bytes]] = {
    "/gbk.txt": ("text/plain; charset=gbk", GBK_BYTES),
    "/utf8.txt": ("text/plain; charset=utf-8", UTF8_BYTES),
    "/sample.docx": (DOCX_CTYPE, DOCX_BYTES),
    "/sample.pptx": (PPTX_CTYPE, PPTX_BYTES),
    "/sample.xlsx": (XLSX_CTYPE, XLSX_BYTES),
    "/sample.pdf": ("application/pdf", PDF_BYTES),
    "/big.bin": ("application/octet-stream", BIG_BYTES),
}

BASE = ""  # main() 里填成 http://127.0.0.1:<port>


# ---------------------------------------------------------------- 本地 HTTP 服务


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/redirect":
            return self._redirect("/gbk.txt")
        if path == "/redirect-evil":
            return self._redirect("http://evil.example.invalid/steal")
        route = ROUTES.get(path)
        if route is None:
            self.send_error(404, "not found")
            return
        content_type, body = route
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, location: str):
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):  # 静音
        pass


# ---------------------------------------------------------------- 测试框架

TESTS = []


def test(fn):
    TESTS.append(fn)
    return fn


def assert_media_error(fn, *args, **kwargs) -> media.MediaError:
    try:
        fn(*args, **kwargs)
    except media.MediaError as exc:
        return exc
    raise AssertionError("预期抛 MediaError，但调用成功了")


# ---------------------------------------------------------------- 用例


@test
def test_fetch_gbk_text():
    data, content_type = media.fetch(BASE + "/gbk.txt")
    assert isinstance(data, bytes) and data == GBK_BYTES
    assert content_type == "text/plain", content_type
    result = media.extract_text(data, "note.txt", content_type)
    assert result["kind"] == "text", result
    assert result["error"] == "", result
    assert "你好，世界" in result["text"], result["text"]
    assert "第二行：文件处理模块" in result["text"], result["text"]
    assert result["pages"] is None
    assert result["truncated"] is False


@test
def test_fetch_utf8_text():
    data, content_type = media.fetch(BASE + "/utf8.txt")
    result = media.extract_text(data, "utf8.txt", content_type)
    assert result["kind"] == "text"
    assert "机器人收到文件后会自动抽取文本" in result["text"], result["text"]


@test
def test_docx():
    data, content_type = media.fetch(BASE + "/sample.docx")
    assert content_type == DOCX_CTYPE
    result = media.extract_text(data, "sample.docx", content_type)
    assert result["kind"] == "docx", result
    assert result["error"] == "", result
    assert "第一章 中文段落" in result["text"], result["text"]
    assert "这是 docx 里的第二段文字。" in result["text"], result["text"]
    assert "特殊字符 & <tag> 测试" in result["text"], result["text"]
    assert result["text"].index("第一章") < result["text"].index("第二段")
    assert result["pages"] is None
    assert result["truncated"] is False


@test
def test_pptx():
    data, content_type = media.fetch(BASE + "/sample.pptx")
    result = media.extract_text(data, "sample.pptx", content_type)
    assert result["kind"] == "pptx", result
    assert result["error"] == "", result
    assert "第一页：项目介绍" in result["text"], result["text"]
    assert "第二页：进度 100%" in result["text"], result["text"]
    assert result["pages"] == 2, result


@test
def test_xlsx():
    data, content_type = media.fetch(BASE + "/sample.xlsx")
    result = media.extract_text(data, "sample.xlsx", content_type)
    assert result["kind"] == "xlsx", result
    assert result["error"] == "", result
    assert "姓名\t数量" in result["text"], result["text"]
    assert "苹果\t12" in result["text"], result["text"]
    assert "香蕉\t7" in result["text"], result["text"]
    assert result["pages"] == 1, result


@test
def test_pdf():
    data, content_type = media.fetch(BASE + "/sample.pdf")
    assert content_type == "application/pdf"
    result = media.extract_text(data, "sample.pdf", content_type)
    assert result["kind"] == "pdf", result
    if media._import_pypdf() is None:
        assert result["error"] == "需要安装 pypdf", result
        assert result["text"] == ""
    else:
        assert result["error"] == "", result
        assert "Hello PDF 12345" in result["text"], result
        assert result["pages"] == 1, result


@test
def test_pdf_without_pypdf():
    original = media._import_pypdf
    media._import_pypdf = lambda: None
    try:
        result = media.extract_text(PDF_BYTES, "sample.pdf", "application/pdf")
    finally:
        media._import_pypdf = original
    assert result["kind"] == "pdf"
    assert result["error"] == "需要安装 pypdf", result
    assert result["text"] == ""
    assert result["pages"] is None


@test
def test_max_bytes():
    assert_media_error(media.fetch, BASE + "/big.bin", max_bytes=4096)
    assert_media_error(media.fetch, BASE + "/big.bin", max_bytes=len(BIG_BYTES) - 1)
    data, _ = media.fetch(BASE + "/big.bin", max_bytes=len(BIG_BYTES))
    assert data == BIG_BYTES
    # 超限错误信息里要带上上限数字，方便上层提示
    exc = assert_media_error(media.fetch, BASE + "/big.bin", max_bytes=1234)
    assert "1234" in str(exc), str(exc)


@test
def test_non_http_urls():
    for url in (
        "",
        "not-a-url",
        "file:///etc/passwd",
        "ftp://127.0.0.1/a.txt",
        "data:text/plain,hi",
        "javascript:alert(1)",
    ):
        assert_media_error(media.fetch, url)


@test
def test_allowed_hosts():
    data, _ = media.fetch(BASE + "/gbk.txt", allowed_hosts=["127.0.0.1"])
    assert data == GBK_BYTES
    assert_media_error(media.fetch, BASE + "/gbk.txt", allowed_hosts=["example.com"])

    # 子域匹配 / 防后缀伪装 / 条目归一化（scheme、端口、大小写、尾部点）
    assert media._host_allowed("sub.example.com", ["example.com"])
    assert media._host_allowed("a.b.example.com", ["*.example.com"])
    assert not media._host_allowed("example.com", ["*.example.com"])
    assert not media._host_allowed("example.com.evil.com", ["example.com"])
    assert not media._host_allowed("badexample.com", ["example.com"])
    assert not media._host_allowed("127.0.0.1", ["example.org"])
    assert media._host_allowed("cdn.example.com", ["https://Example.COM:8443"])
    assert media._host_allowed("127.0.0.1", ["127.0.0.1"])


@test
def test_redirects():
    data, _ = media.fetch(BASE + "/redirect", allowed_hosts=["127.0.0.1"])
    assert data == GBK_BYTES
    assert_media_error(media.fetch, BASE + "/redirect-evil", allowed_hosts=["127.0.0.1"])


@test
def test_kind_of():
    assert media.kind_of("a.JPG") == "image"
    assert media.kind_of("pic.png", "application/octet-stream") == "image"
    assert media.kind_of("https://cdn.example.com/files/a.docx?sign=1") == "docx"
    assert media.kind_of("x.bin", "image/png") == "image"
    assert media.kind_of("noext", "application/pdf") == "pdf"
    assert media.kind_of("noext", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet") == "xlsx"
    assert media.kind_of("note.txt") == "text"
    assert media.kind_of("code.py") == "text"
    assert media.kind_of("data.csv", "application/octet-stream") == "text"
    assert media.kind_of("", "text/plain; charset=utf-8") == "text"
    assert media.kind_of("x.zip") == "unknown"
    assert media.kind_of("x.doc") == "unknown"  # 旧版二进制 doc 不支持


@test
def test_image_and_unknown():
    image = media.extract_text(b"\x89PNG\r\n\x1a\nxxxx", "pic.png", "image/png")
    assert image == {
        "kind": "image",
        "text": "",
        "pages": None,
        "truncated": False,
        "error": "",
    }, image

    unknown = media.extract_text(b"PK\x03\x04", "archive.zip", "application/zip")
    assert unknown["kind"] == "unknown", unknown
    assert unknown["text"] == ""
    assert unknown["error"] != ""


@test
def test_truncation():
    long_text = ("字" * 100).encode("utf-8")
    result = media.extract_text(long_text, "long.txt", "text/plain", max_chars=10)
    assert result["truncated"] is True, result
    assert len(result["text"]) == 10, result
    assert result["text"] == "字" * 10

    exact = media.extract_text(b"12345", "x.txt", "", max_chars=5)
    assert exact["truncated"] is False and exact["text"] == "12345", exact


@test
def test_corrupt_files_do_not_raise():
    broken_docx = media.extract_text(b"definitely not a zip", "bad.docx", "application/octet-stream")
    assert broken_docx["kind"] == "docx"
    assert broken_docx["text"] == ""
    assert broken_docx["error"] != ""

    broken_pdf = media.extract_text(b"not a pdf at all", "bad.pdf", "application/pdf")
    assert broken_pdf["kind"] == "pdf"
    assert broken_pdf["error"] != ""

    empty = media.extract_text(b"", "empty.txt")
    assert set(empty) == {"kind", "text", "pages", "truncated", "error"}
    assert empty["text"] == "" and empty["error"] == ""


# ---------------------------------------------------------------- 入口


def main() -> int:
    global BASE

    logging.getLogger("pypdf").setLevel(logging.ERROR)  # 损坏 PDF 用例会打警告，静音

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    BASE = "http://127.0.0.1:%d" % server.server_address[1]

    passed = 0
    failed = 0
    try:
        for index, fn in enumerate(TESTS, 1):
            name = fn.__name__.replace("test_", "", 1)
            try:
                fn()
            except Exception as exc:  # 断言失败 / 意外异常都算 FAIL
                failed += 1
                print("[FAIL] %02d %-28s %s: %s" % (index, name, type(exc).__name__, exc))
            else:
                passed += 1
                print("[PASS] %02d %s" % (index, name))
    finally:
        server.shutdown()
        server.server_close()

    total = passed + failed
    print("-" * 46)
    print("%d passed, %d failed, %d total" % (passed, failed, total))
    pypdf_ok = media._import_pypdf() is not None
    print("pypdf: %s" % ("已安装" if pypdf_ok else "未安装（PDF 走 error 分支）"))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
