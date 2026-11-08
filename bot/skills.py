"""技能（skills）系统：给 AI 读取的 Markdown 技能说明。

技能目录：``<data_dir>/skills/``（机器人默认 ``bot/data/skills``，已被 .gitignore 忽略）。
- 每个技能是一个 ``*.md`` 文件，文件名（去 .md）就是技能名；
- 第一行非空文本（去掉开头的 #）作为描述；
- ``read_skill`` 只允许读该目录下的 .md，防路径穿越，≤200KB。

只依赖 Python 标准库。
"""
from __future__ import annotations

from pathlib import Path

MAX_SKILL_BYTES = 200 * 1024
SKILLS_DIR_NAME = "skills"
DESCRIPTION_MAX_CHARS = 200

__all__ = [
    "MAX_SKILL_BYTES",
    "SKILLS_DIR_NAME",
    "skills_dir",
    "list_skills",
    "read_skill",
    "skills_index_text",
]


def skills_dir(data_dir=None) -> Path:
    base = Path(data_dir) if data_dir is not None else (Path(__file__).resolve().parent / "data")
    return base / SKILLS_DIR_NAME


def _clean_name(name) -> str | None:
    """校验技能名：只允许纯文件名（防穿越），可带 .md 后缀。"""
    text = str(name or "").strip()
    if not text:
        return None
    stem = text[:-3] if text.lower().endswith(".md") else text
    stem = stem.strip()
    if not stem or len(stem) > 120:
        return None
    if stem in (".", "..") or "/" in stem or "\\" in stem or "\x00" in stem:
        return None
    if Path(stem).name != stem or ".." in stem:
        return None
    return stem


def _first_line(text: str) -> str:
    for line in str(text or "").splitlines():
        value = line.strip()
        if not value:
            continue
        value = value.lstrip("#").strip()
        if value:
            return value[:DESCRIPTION_MAX_CHARS]
    return ""


def list_skills(data_dir=None) -> list[dict]:
    """返回技能列表：[{name, description}]（按名字排序，跳过非法/超限文件）。"""
    root = skills_dir(data_dir)
    try:
        if not root.is_dir():
            return []
        paths = sorted(root.glob("*.md"), key=lambda p: p.name.lower())
    except OSError:
        return []
    out: list[dict] = []
    for path in paths:
        try:
            if not path.is_file():
                continue
            if path.stat().st_size > MAX_SKILL_BYTES:
                continue
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        out.append({"name": path.stem, "description": _first_line(text)})
    return out


def read_skill(name, data_dir=None) -> str | None:
    """安全读取一个技能 .md 的完整内容；不存在/非法/超限返回 None。"""
    stem = _clean_name(name)
    if stem is None:
        return None
    root = skills_dir(data_dir)
    path = root / f"{stem}.md"
    try:
        root_resolved = root.resolve()
        target = path.resolve()
        target.relative_to(root_resolved)
    except (OSError, ValueError):
        return None
    try:
        if not target.is_file():
            return None
        if target.stat().st_size > MAX_SKILL_BYTES:
            return None
        return target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def skills_index_text(data_dir=None) -> str:
    """技能目录的「name — description」文本（给 AI system prompt 用）。"""
    items = list_skills(data_dir)
    return "\n".join(
        f"- {item['name']} — {item['description']}".rstrip(" —")
        for item in items
    )
