"""文件 / 图片下载与文本抽取（不依赖 QQ 框架，可在 proot 里单独使用）。

对外接口：
    fetch(url, max_bytes=20_000_000, timeout=20.0, allowed_hosts=None)
        -> (bytes, content_type)
    kind_of(filename, content_type="") -> str
    extract_text(data, filename="", content_type="", max_chars=6000) -> dict
    describe_saved(paths) -> str

设计要点：
- fetch 只允许 http / https，file://、ftp://、data: 等一律拒绝；重定向逐跳校验，
  allowed_hosts 非空时只放行白名单主机（子域也算），用于防 SSRF；
- 下载上限 max_bytes：先看 Content-Length，再边读边累计，超限抛 MediaError；
  max_bytes <= 0 表示不限制（账号套餐模式用），默认仍是 20MB；
- 默认 20s 超时，全程内存处理，不落盘；
- docx / pptx / xlsx 只用标准库 zipfile + xml.etree 解析 OOXML；
- pdf 依赖可选的 pypdf，没装时返回 error="需要安装 pypdf"，不抛异常；
- extract_text 永远返回全部 5 个键，解析失败也只写进 error，不向外抛异常。
"""
from __future__ import annotations

import io
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

__all__ = ["MediaError", "fetch", "kind_of", "extract_text", "describe_saved"]

DEFAULT_MAX_BYTES = 20_000_000
DEFAULT_TIMEOUT = 20.0
DEFAULT_MAX_CHARS = 6000

_CHUNK = 64 * 1024
# 单个 OOXML 内部部件解压后的上限，防 zip 炸弹
_ZIP_MAX_MEMBER = 64 * 1024 * 1024

_USER_AGENT = "QQBot-Media/1.0 (+https://github.com/)"

# ---------------------------------------------------------------- 异常


class MediaError(Exception):
    """媒体下载 / 解析过程中的可预期错误（非 http(s)、超限、HTTP 失败等）。"""


# ---------------------------------------------------------------- URL / 白名单


def _normalize_host(value) -> str:
    """把 host 条目 / hostname 归一化成小写、无端口、无尾部点。"""
    if not isinstance(value, str):
        return ""
    value = value.strip().lower()
    if not value:
        return ""
    if "://" in value:
        try:
            value = urllib.parse.urlsplit(value).hostname or ""
        except ValueError:
            return ""
    value = value.strip().rstrip(".")
    if not value:
        return ""
    if value.startswith("["):  # [::1] -> ::1
        return value.strip("[]")
    # 裸 IPv6 有多个冒号，不能按端口拆
    if value.count(":") == 1:
        value = value.split(":", 1)[0]
    return value


def _host_allowed(host: str, allowed_hosts) -> bool:
    """host 是否命中白名单（精确匹配或子域匹配，支持 *.example.com）。"""
    h = _normalize_host(host)
    if not h:
        return False
    for entry in allowed_hosts or ():
        rule = _normalize_host(entry)
        if not rule:
            continue
        if rule.startswith("*."):
            base = rule[2:]
            if base and h.endswith("." + base):
                return True
        elif h == rule or h.endswith("." + rule):
            return True
    return False


def _validate_url(url: str, allowed_hosts=None) -> str:
    if not isinstance(url, str) or not url.strip():
        raise MediaError("URL 为空")
    url = url.strip()
    if any(ord(ch) < 0x20 for ch in url):
        raise MediaError("URL 含非法控制字符")
    try:
        parts = urllib.parse.urlsplit(url)
        scheme = (parts.scheme or "").lower()
        host = parts.hostname
    except ValueError as e:
        raise MediaError(f"URL 格式错误: {e}") from e
    if scheme not in ("http", "https"):
        raise MediaError(f"只允许 http/https 链接，收到: {scheme or '空'}")
    if not host:
        raise MediaError("URL 缺少主机名")
    if allowed_hosts and not _host_allowed(host, allowed_hosts):
        raise MediaError(f"目标主机不在白名单: {host}")
    return url


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """重定向时重新校验协议与白名单，避免 302 绕过 SSRF 限制。"""

    def __init__(self, allowed_hosts=None):
        super().__init__()
        self.allowed_hosts = allowed_hosts

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        _validate_url(newurl, self.allowed_hosts)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


# ---------------------------------------------------------------- 下载


def fetch(url: str, max_bytes: int = DEFAULT_MAX_BYTES, timeout: float = DEFAULT_TIMEOUT,
          allowed_hosts: list | None = None) -> tuple[bytes, str]:
    """下载 URL，返回 (data, content_type)。

    - data 全在内存里，不写磁盘；
    - 只允许 http(s)，否则抛 MediaError；
    - 超过 max_bytes 抛 MediaError（Content-Length 超限会提前拒绝）；
    - max_bytes <= 0 表示不限制大小（账号套餐模式），默认 20MB；
    - allowed_hosts 非空时只允许这些 host（含子域），重定向也会校验。
    """
    url = _validate_url(url, allowed_hosts)
    try:
        max_bytes = int(max_bytes)
        timeout = float(timeout)
    except (TypeError, ValueError) as e:
        raise MediaError("max_bytes / timeout 参数不合法") from e
    if timeout <= 0:
        raise MediaError("timeout 必须大于 0")

    opener = urllib.request.build_opener(_SafeRedirectHandler(allowed_hosts))
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": _USER_AGENT,
            "Accept": "*/*",
            "Accept-Encoding": "identity",
        },
        method="GET",
    )

    try:
        with opener.open(req, timeout=timeout) as resp:
            raw_ctype = resp.headers.get("Content-Type") or ""
            content_type = raw_ctype.split(";", 1)[0].strip().lower() or "application/octet-stream"

            # max_bytes <= 0 表示不限制（账号套餐模式）；正数才做上限检查。
            unlimited = max_bytes <= 0
            length = (resp.headers.get("Content-Length") or "").strip()
            if not unlimited and length.isdigit() and int(length) > max_bytes:
                raise MediaError(f"文件超过大小上限（上限 {max_bytes} 字节）")

            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = resp.read(_CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if not unlimited and total > max_bytes:
                    raise MediaError(f"文件超过大小上限（上限 {max_bytes} 字节）")
                chunks.append(chunk)
    except MediaError:
        raise
    except urllib.error.HTTPError as e:
        raise MediaError(f"下载失败：HTTP {e.code} {e.reason}") from e
    except urllib.error.URLError as e:
        raise MediaError(f"下载失败：{e.reason}") from e
    except (socket.timeout, TimeoutError):
        raise MediaError(f"下载超时（{timeout:g}s）") from None
    except Exception as e:  # http.client.HTTPException / ConnectionError 等
        raise MediaError(f"下载失败：{e}") from e

    return b"".join(chunks), content_type


# ---------------------------------------------------------------- 类型判断

_EXT_KIND = {
    # 图片
    ".jpg": "image", ".jpeg": "image", ".png": "image", ".gif": "image",
    ".webp": "image", ".bmp": "image", ".tif": "image", ".tiff": "image",
    ".heic": "image", ".heif": "image", ".avif": "image", ".ico": "image",
    ".svg": "image",
    # 文本 / 代码
    ".txt": "text", ".text": "text", ".md": "text", ".markdown": "text",
    ".csv": "text", ".tsv": "text", ".json": "text", ".jsonl": "text",
    ".ndjson": "text", ".log": "text", ".xml": "text", ".yaml": "text",
    ".yml": "text", ".toml": "text", ".ini": "text", ".cfg": "text",
    ".conf": "text", ".properties": "text", ".rst": "text", ".tex": "text",
    ".py": "text", ".pyw": "text", ".js": "text", ".mjs": "text",
    ".cjs": "text", ".ts": "text", ".tsx": "text", ".jsx": "text",
    ".java": "text", ".kt": "text", ".c": "text", ".h": "text",
    ".cc": "text", ".cpp": "text", ".hpp": "text", ".cs": "text",
    ".go": "text", ".rs": "text", ".rb": "text", ".php": "text",
    ".sh": "text", ".bash": "text", ".zsh": "text", ".bat": "text",
    ".ps1": "text", ".sql": "text", ".html": "text", ".htm": "text",
    ".css": "text", ".scss": "text", ".less": "text", ".srt": "text",
    ".ass": "text", ".vtt": "text", ".lrc": "text",
    # 文档
    ".docx": "docx", ".pptx": "pptx", ".xlsx": "xlsx", ".pdf": "pdf",
    ".doc": "doc", ".wps": "doc", ".rtf": "doc",
}

_CTYPE_KIND = {
    "application/pdf": "pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "application/json": "text",
    "application/ld+json": "text",
    "application/xml": "text",
    "application/xhtml+xml": "text",
    "application/javascript": "text",
    "application/x-javascript": "text",
    "application/ecmascript": "text",
    "application/x-yaml": "text",
    "application/yaml": "text",
    "application/csv": "text",
    "application/sql": "text",
}


def _basename(filename) -> str:
    """从文件名或 URL 里取 basename（去掉 query / fragment / 目录）。"""
    name = str(filename or "").strip()
    if not name:
        return ""
    if "://" in name:
        try:
            name = urllib.parse.urlsplit(name).path or ""
        except ValueError:
            name = name.split("?", 1)[0].split("#", 1)[0]
    else:
        name = name.split("?", 1)[0].split("#", 1)[0]
    name = name.replace("\\", "/").rstrip("/")
    return name.rsplit("/", 1)[-1]


def kind_of(filename: str, content_type: str = "") -> str:
    """返回 'image' | 'text' | 'docx' | 'pptx' | 'xlsx' | 'pdf' | 'unknown'。

    先按扩展名判断，再退回 Content-Type；两者都没有则 unknown。
    """
    name = _basename(filename).lower()
    ext = ""
    if "." in name:
        ext = "." + name.rsplit(".", 1)[-1]
    kind = _EXT_KIND.get(ext)
    if kind:
        return kind

    ctype = str(content_type or "").split(";", 1)[0].strip().lower()
    if ctype:
        if ctype.startswith("image/"):
            return "image"
        kind = _CTYPE_KIND.get(ctype)
        if kind:
            return kind
        if ctype.startswith("text/"):
            return "text"
        if ctype.endswith("+json") or ctype.endswith("+xml"):
            return "text"
    return "unknown"


# ---------------------------------------------------------------- 文本解码


def _decode_text(data: bytes) -> str:
    """按 utf-8 → gbk → latin-1 的顺序解码（带 BOM 的 UTF-16 优先）。"""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return data.decode("utf-16")
        except UnicodeDecodeError:
            pass
    for encoding in ("utf-8-sig", "gbk", "latin-1"):
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("latin-1", "replace")


# ---------------------------------------------------------------- OOXML 解析

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"

_SLIDE_RE = re.compile(r"^ppt/slides/slide(\d+)\.xml$")
_SHEET_RE = re.compile(r"^xl/worksheets/sheet(\d+)\.xml$")


def _zip_open(data: bytes) -> zipfile.ZipFile:
    try:
        return zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise MediaError("文件不是有效的 Office 文档（zip 解包失败）") from e


def _read_member(zf: zipfile.ZipFile, name: str) -> bytes:
    try:
        info = zf.getinfo(name)
    except KeyError as e:
        raise MediaError(f"Office 文档缺少 {name}") from e
    if info.file_size > _ZIP_MAX_MEMBER:
        raise MediaError(f"文档内部文件过大：{name}")
    try:
        return zf.read(name)
    except Exception as e:  # CRC 错误等
        raise MediaError(f"读取 {name} 失败: {e}") from e


def _parse_xml(raw: bytes, label: str) -> ET.Element:
    try:
        return ET.fromstring(raw)
    except ET.ParseError as e:
        raise MediaError(f"{label} XML 解析失败: {e}") from e


def _extract_paragraphs(root: ET.Element, ns: str, text_tag: str = "t",
                        tab_tag: str | None = "tab") -> list[str]:
    """按段落（p）汇总文本：<w:t>/<a:t> 拼接，tab / br 转成制表 / 换行。"""
    lines: list[str] = []
    for para in root.iter(ns + "p"):
        buf: list[str] = []
        for node in para.iter():
            tag = node.tag
            if tag == ns + text_tag:
                buf.append(node.text or "")
            elif tab_tag and tag == ns + tab_tag:
                buf.append("\t")
            elif tag in (ns + "br", ns + "cr"):
                buf.append("\n")
        lines.append("".join(buf))
    return lines


def _docx_text(data: bytes) -> str:
    with _zip_open(data) as zf:
        root = _parse_xml(_read_member(zf, "word/document.xml"), "word/document.xml")
    return "\n".join(_extract_paragraphs(root, _W))


def _pptx_text(data: bytes) -> tuple[str, int]:
    with _zip_open(data) as zf:
        slides: list[tuple[int, str]] = []
        for name in zf.namelist():
            m = _SLIDE_RE.match(name)
            if m:
                slides.append((int(m.group(1)), name))
        if not slides:
            raise MediaError("PPTX 里没找到幻灯片")
        slides.sort()
        pages: list[str] = []
        for _num, name in slides:
            root = _parse_xml(_read_member(zf, name), name)
            # 形状里的段落可以直接遍历 a:p，不需要关心 slide 层级
            pages.append("\n".join(_extract_paragraphs(root, _A)))
    return "\n\n".join(pages), len(pages)


def _si_text(si: ET.Element) -> str:
    """sharedStrings / inlineStr 里的 <si>：直接 <t> 或富文本 <r><t> 拼接。"""
    parts: list[str] = []
    for child in si:
        if child.tag == _S + "t":
            parts.append(child.text or "")
        elif child.tag == _S + "r":
            for t in child.iter(_S + "t"):
                parts.append(t.text or "")
    return "".join(parts)


def _col_index(ref: str) -> int | None:
    """A1 -> 0, B1 -> 1, AA1 -> 26；解析失败返回 None。"""
    m = re.match(r"([A-Za-z]+)", ref or "")
    if not m:
        return None
    index = 0
    for ch in m.group(1).upper():
        index = index * 26 + (ord(ch) - 64)
    return index - 1


def _cell_value(cell: ET.Element, shared: list[str]) -> str:
    ctype = cell.get("t") or ""
    value = cell.find(_S + "v")
    raw = (value.text or "") if value is not None else ""
    if ctype == "s":
        try:
            return shared[int(raw.strip())]
        except (ValueError, IndexError):
            return ""
    if ctype == "inlineStr":
        inline = cell.find(_S + "is")
        return _si_text(inline) if inline is not None else ""
    if ctype == "b":
        return "TRUE" if raw.strip() == "1" else "FALSE"
    return raw


def _xlsx_text(data: bytes) -> tuple[str, int]:
    with _zip_open(data) as zf:
        names = zf.namelist()

        shared: list[str] = []
        if "xl/sharedStrings.xml" in names:
            root = _parse_xml(_read_member(zf, "xl/sharedStrings.xml"), "xl/sharedStrings.xml")
            shared = [_si_text(si) for si in root.iter(_S + "si")]

        sheets: list[tuple[int, str]] = []
        for name in names:
            m = _SHEET_RE.match(name)
            if m:
                sheets.append((int(m.group(1)), name))
        if not sheets:
            raise MediaError("XLSX 里没找到工作表")
        sheets.sort()

        sheet_texts: list[str] = []
        for _num, name in sheets:
            root = _parse_xml(_read_member(zf, name), name)
            lines: list[str] = []
            for row in root.iter(_S + "row"):
                cells: dict[int, str] = {}
                max_index = -1
                for cell in row.findall(_S + "c"):
                    index = _col_index(cell.get("r") or "")
                    if index is None:
                        index = max_index + 1
                    cells[index] = _cell_value(cell, shared)
                    max_index = max(max_index, index)
                if max_index < 0:
                    lines.append("")
                else:
                    lines.append("\t".join(cells.get(i, "") for i in range(max_index + 1)))
            sheet_texts.append("\n".join(lines))
    return "\n\n".join(sheet_texts), len(sheet_texts)


# ---------------------------------------------------------------- PDF


def _import_pypdf():
    """可替换的 pypdf 加载点（测试里可以 patch 成返回 None 来验证缺库分支）。"""
    try:
        import pypdf  # noqa: PLC0415
        return pypdf
    except Exception:
        return None


def _pdf_text(data: bytes) -> tuple[str, int | None, str]:
    """返回 (text, pages, error)。没装 pypdf 时返回提示而不是抛异常。"""
    pypdf = _import_pypdf()
    if pypdf is None:
        return "", None, "需要安装 pypdf"
    try:
        reader = pypdf.PdfReader(io.BytesIO(data))
    except Exception as e:
        return "", None, f"PDF 解析失败: {e}"
    try:
        if getattr(reader, "is_encrypted", False):
            try:
                reader.decrypt("")
            except Exception:
                return "", None, "PDF 已加密，无法提取文本"
        pages: list[str] = []
        for page in reader.pages:
            try:
                pages.append(page.extract_text() or "")
            except Exception:
                pages.append("")
        return "\n".join(pages), len(reader.pages), ""
    except Exception as e:
        return "", None, f"PDF 解析失败: {e}"


# ---------------------------------------------------------------- 文本抽取


def _doc_text(data: bytes) -> str:
    """老式 .doc/.wps/.rtf：优先 antiword，其次从二进制里捞 UTF-16/GBK 文本。"""
    if data[:5] == b"{\\rtf":
        raw = data.decode("latin-1", "ignore")
        raw = re.sub(r"\\\\[a-zA-Z]+-?\d* ?", " ", raw)
        raw = raw.replace("{", " ").replace("}", " ")
        return re.sub(r"[ \t]{2,}", " ", raw).strip()
    import shutil, subprocess, tempfile, os
    tool = shutil.which("antiword") or shutil.which("catdoc")
    if tool:
        fd, path = tempfile.mkstemp(suffix=".doc")
        try:
            os.write(fd, data); os.close(fd)
            out = subprocess.run([tool, path], capture_output=True, timeout=20)
            if out.returncode == 0 and out.stdout.strip():
                return out.stdout.decode("utf-8", "ignore")
        except Exception:
            pass
        finally:
            try: os.unlink(path)
            except Exception: pass
    # 兜底：WordDocument 里正文常为 UTF-16LE，捞可打印片段
    best = []
    candidates = []
    for enc in ("utf-16-le", "gbk"):
        for skip in (0, 1):
            try:
                candidates.append(data[skip:].decode(enc, "ignore"))
            except Exception:
                continue
    for txt in candidates:
        runs = re.findall(r"[\u4e00-\u9fff\u3000-\u303fA-Za-z0-9，。；：！？、（）《》%.\-\s]{6,}", txt)
        joined = "\n".join(r.strip() for r in runs if len(r.strip()) >= 6)
        score = len(re.findall(r"[\u4e00-\u9fff]", joined))
        if score > len(re.findall(r"[\u4e00-\u9fff]", "".join(best))):
            best = [joined]
    # 去掉二进制噪声：只保留可读行
    text = "".join(best)
    lines = []
    for line in text.splitlines():
        line = " ".join(line.split())
        if len(re.findall(r"[\u4e00-\u9fffA-Za-z]", line)) >= 4:
            lines.append(line)
    return "\n".join(lines)


def extract_text(data: bytes, filename: str = "", content_type: str = "",
                 max_chars: int = DEFAULT_MAX_CHARS) -> dict:
    """从文件字节里抽文本。

    返回 {"kind", "text", "pages", "truncated", "error"}，5 个键始终存在；
    解析失败只写 error，不向外抛异常。
    """
    if data is None:
        data = b""
    if not isinstance(data, (bytes, bytearray, memoryview)):
        data = bytes(data)
    data = bytes(data)

    kind = kind_of(filename, content_type)
    text = ""
    pages: int | None = None
    error = ""

    try:
        if kind == "text":
            text = _decode_text(data)
        elif kind == "docx":
            text = _docx_text(data)
        elif kind == "pptx":
            text, pages = _pptx_text(data)
        elif kind == "xlsx":
            text, pages = _xlsx_text(data)
        elif kind == "pdf":
            text, pages, error = _pdf_text(data)
        elif kind == "doc":
            text = _doc_text(data)
        elif kind == "image":
            pass  # 图片不抽文本，由上层做视觉处理
        else:
            error = "不支持的文件类型，无法提取文本"
    except MediaError as e:
        error = str(e)
    except Exception as e:  # 任何解析意外都不向外抛
        error = f"解析失败: {e}"

    text = text.replace("\r\n", "\n").replace("\r", "\n").strip("\n")

    try:
        limit = int(max_chars)
    except (TypeError, ValueError):
        limit = DEFAULT_MAX_CHARS
    limit = max(0, limit)
    truncated = len(text) > limit
    if truncated:
        text = text[:limit]

    return {
        "kind": kind,
        "text": text,
        "pages": pages,
        "truncated": truncated,
        "error": error,
    }


# ---------------------------------------------------------------- 杂项


def _human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{size} B"


def describe_saved(paths) -> str:
    """可选辅助：把已保存的文件路径列表写成人话，例如『已保存：a.png（1.2 MB）』。"""
    if paths is None:
        return ""
    if isinstance(paths, (str, bytes, Path)):
        paths = [paths]
    try:
        items = list(paths)
    except TypeError:
        return ""
    parts: list[str] = []
    for item in items:
        try:
            path = Path(item)
            parts.append(f"{path.name}（{_human_size(path.stat().st_size)}）")
        except Exception:
            parts.append(str(item))
    if not parts:
        return ""
    return "已保存：" + "、".join(parts)
