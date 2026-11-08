"""文件生成与发送：把文本/代码保存成本地文件，再通过 OneBot 发到群/私聊。

NapCat 支持 upload_group_file / upload_private_file，file 参数可用本机绝对路径。
"""
from __future__ import annotations

import re
import time
from pathlib import Path

LANG_EXT = {
    "python": ".py", "py": ".py", "javascript": ".js", "js": ".js", "typescript": ".ts",
    "ts": ".ts", "java": ".java", "kotlin": ".kt", "c": ".c", "cpp": ".cpp", "c++": ".cpp",
    "csharp": ".cs", "cs": ".cs", "go": ".go", "rust": ".rs", "rs": ".rs", "php": ".php",
    "ruby": ".rb", "rb": ".rb", "shell": ".sh", "bash": ".sh", "sh": ".sh", "zsh": ".sh",
    "powershell": ".ps1", "sql": ".sql", "html": ".html", "htm": ".html", "svg": ".svg", "css": ".css", "scss": ".scss",
    "json": ".json", "yaml": ".yml", "yml": ".yml", "toml": ".toml", "xml": ".xml",
    "markdown": ".md", "md": ".md", "text": ".txt", "txt": ".txt", "docker": ".dockerfile",
    "dockerfile": ".dockerfile", "lua": ".lua", "dart": ".dart", "swift": ".swift",
    "r": ".r", "matlab": ".m", "vue": ".vue", "jsx": ".jsx", "tsx": ".tsx",
}

_BLOCK_RE = re.compile(r"```([A-Za-z0-9_+#.-]*)\n(.*?)```", re.S)
_FENCE_RE = re.compile(r"^```[^\n]*\n", re.M)


def extract_code_blocks(text: str) -> list[dict]:
    """从回复里提取代码块： [{"lang", "code", "lines", "chars"}]"""
    blocks = []
    for m in _BLOCK_RE.finditer(text or ""):
        lang = (m.group(1) or "").strip().lower()
        code = m.group(2)
        if not code.strip():
            continue
        blocks.append(
            {
                "raw": m.group(0),
                "lang": lang,
                "code": code.rstrip("\n") + "\n",
                "lines": code.count("\n") + 1,
                "chars": len(code),
            }
        )
    return blocks


def ext_for(lang: str) -> str:
    lang = (lang or "").strip().lower()
    if lang in LANG_EXT:
        return LANG_EXT[lang]
    # ```python title=xx``` 这类写法，取第一个词
    first = lang.split()[0] if lang.split() else ""
    return LANG_EXT.get(first, ".txt")


class FileStore:
    def __init__(self, base_dir: Path):
        self.dir = Path(base_dir) / "files"
        self.dir.mkdir(parents=True, exist_ok=True)

    def save_text(self, content: str, name: str) -> Path:
        name = re.sub(r"[\\/:*?\"<>|]", "_", name)[:80]
        path = self.dir / name
        path.write_text(content, encoding="utf-8")
        return path

    def save_bytes(self, data: bytes, name: str) -> Path:
        name = re.sub(r"[\\/:*?\"<>|]", "_", name)[:80]
        path = self.dir / name
        path.write_bytes(data)
        return path

    def cleanup(self, keep: int = 50):
        try:
            files = sorted(self.dir.glob("*"), key=lambda p: p.stat().st_mtime, reverse=True)
            for f in files[keep:]:
                f.unlink(missing_ok=True)
        except Exception:
            pass


def make_filename(lang: str, prefix: str = "code", index: int = 0) -> str:
    stamp = time.strftime("%m%d_%H%M%S")
    suffix = f"_{index}" if index else ""
    return f"{prefix}_{stamp}{suffix}{ext_for(lang)}"


async def send_file(ctx, event, path: Path, name: str | None = None) -> bool:
    """把本地文件发到消息来源（群/私聊）。"""
    name = name or Path(path).name
    try:
        if event.get("message_type") == "group":
            await ctx.api.call(
                "upload_group_file",
                group_id=int(event.get("group_id")),
                file=str(path),
                name=name,
            )
        else:
            await ctx.api.call(
                "upload_private_file",
                user_id=int(event.get("user_id")),
                file=str(path),
                name=name,
            )
        ctx.log.info("已发送文件 %s -> %s", name, event.get("group_id") or event.get("user_id"))
        return True
    except Exception as e:
        ctx.log.warning("发送文件失败 %s: %s", name, e)
        return False
