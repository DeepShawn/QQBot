#!/usr/bin/env python3
"""QQ 机器人代码自动更新（管理台常驻进程里跑）。

从 GitHub 查 DeepShawn/QQBot main 分支最新 commit，下载源码 tar.gz，
安全解压后同步 bot/、scripts/、start-all.sh，保留用户数据
（bot/config.json、bot/data/、bot/logs/ 等），并记录已安装 commit 到
bot/.update-meta.json。

约束：
- 只用标准库；
- 所有异常转成中文 UpdaterError，不向接口抛原生异常/堆栈/token；
- 更新在后台线程里跑，同一时间只允许一个；
- 不自动重启，状态里给 restart_required=True 让界面提示。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path, PurePosixPath

# updater.py 在 bot/ 下：仓库根是上一级
BOT_DIR = Path(__file__).resolve().parent          # qqbot/bot
ROOT = BOT_DIR.parent                              # qqbot/
META_PATH = BOT_DIR / ".update-meta.json"

REPO = "DeepShawn/QQBot"
BRANCH = "main"
COMMIT_API = f"https://api.github.com/repos/{REPO}/commits/{BRANCH}"
ARCHIVE_URL = f"https://github.com/{REPO}/archive/refs/heads/{BRANCH}.tar.gz"
USER_AGENT = "QQBot-Updater/1.0"

HTTP_TIMEOUT = 15
DOWNLOAD_TIMEOUT = 90
DOWNLOAD_CHUNK = 64 * 1024

# 状态机：idle -> checking -> downloading -> extracting -> syncing -> done/failed
PHASES = ("idle", "checking", "downloading", "extracting", "syncing", "done", "failed")

# bot/ 下同步时保留的用户内容 / 运行时产物
BOT_IGNORE_NAMES = {"config.json", "data", "logs", "__pycache__"}


class UpdaterError(Exception):
    """给用户看的中文短句。"""


# ---------------------------------------------------------------- 状态机
_state_lock = threading.Lock()
_start_lock = threading.Lock()
_worker: threading.Thread | None = None
_state: dict = {
    "phase": "idle",
    "message": "",
    "error": None,
    "restart_required": False,
    "updated_at": 0.0,
}


def _set_state(**kw):
    with _state_lock:
        _state.update(kw)
        _state["updated_at"] = time.time()


def status() -> dict:
    """当前更新状态（深拷贝，调用方可随意读）。"""
    with _state_lock:
        return dict(_state)


# ---------------------------------------------------------------- 本地记录
def read_meta() -> dict:
    """读 bot/.update-meta.json；坏文件当作没有记录。"""
    try:
        data = json.loads(META_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _write_meta(data: dict) -> None:
    try:
        META_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = META_PATH.with_name(META_PATH.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, META_PATH)
    except Exception:
        raise UpdaterError("保存更新记录失败（检查磁盘空间和权限）") from None


def current_commit() -> str:
    return str(read_meta().get("commit") or "").strip()


def _short(sha: str) -> str:
    return str(sha or "")[:7]


# ---------------------------------------------------------------- HTTP
def _http_json(url: str, timeout: float = HTTP_TIMEOUT) -> dict:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/vnd.github+json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(2 << 20)
    except urllib.error.HTTPError as e:
        code = int(getattr(e, "code", 0) or 0)
        if code in (403, 429):
            raise UpdaterError("查询太频繁了，GitHub 限制每小时 60 次，等一会儿再试") from None
        if code == 404:
            raise UpdaterError("GitHub 上找不到这个仓库，检查项目地址") from None
        raise UpdaterError(f"查询最新版本失败（HTTP {code}），稍后再试") from None
    except Exception:
        raise UpdaterError("网络连不上，检查网络后再试") from None
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        raise UpdaterError("版本信息格式不对，稍后再试") from None
    if not isinstance(data, dict):
        raise UpdaterError("版本信息格式不对，稍后再试")
    return data


def _fetch_latest_commit() -> dict:
    """GitHub API 取 main 分支最新 commit。"""
    data = _http_json(COMMIT_API)
    sha = str(data.get("sha") or "").strip()
    if not sha:
        raise UpdaterError("没读到最新版本号，稍后再试")
    commit = data.get("commit") if isinstance(data.get("commit"), dict) else {}
    lines = str((commit or {}).get("message") or "").strip().splitlines()
    title = lines[0][:120] if lines else ""
    url = str(data.get("html_url") or f"https://github.com/{REPO}/commit/{sha}")
    return {"commit": sha, "title": title, "url": url}


def _download(url: str, dest: Path) -> int:
    """下载 tar.gz 到 dest，返回字节数。"""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    total = 0
    try:
        with urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT) as resp:
            with open(dest, "wb") as f:
                while True:
                    chunk = resp.read(DOWNLOAD_CHUNK)
                    if not chunk:
                        break
                    f.write(chunk)
                    total += len(chunk)
                    if total % (512 * 1024) < DOWNLOAD_CHUNK:
                        _set_state(message=f"正在下载更新包…（已下载 {total // 1024} KB）")
    except urllib.error.HTTPError as e:
        code = int(getattr(e, "code", 0) or 0)
        raise UpdaterError(f"下载更新包失败（HTTP {code}），稍后再试") from None
    except Exception:
        raise UpdaterError("下载更新包失败，检查网络后再试") from None
    if total <= 0:
        raise UpdaterError("下载的更新包是空的，稍后再试")
    return total


# ---------------------------------------------------------------- 安全解压
def _is_within(path, base) -> bool:
    try:
        Path(path).resolve().relative_to(Path(base).resolve())
        return True
    except Exception:
        return False


def _check_member(member: tarfile.TarInfo, base: Path) -> None:
    """拒绝绝对路径、..、越界符号链接/硬链接、设备文件等。"""
    name = member.name or ""
    if not name or "\x00" in name or name.startswith("/") or name.startswith("\\"):
        raise UpdaterError("更新包里有不安全的路径，已取消更新")
    pure = PurePosixPath(name.replace("\\", "/"))
    if pure.is_absolute() or any(part in ("..", "") for part in pure.parts):
        raise UpdaterError("更新包里有不安全的路径，已取消更新")
    if not _is_within(base / pure, base):
        raise UpdaterError("更新包里的文件会写到目录外，已取消更新")
    if member.issym() or member.islnk():
        link = (member.linkname or "").replace("\\", "/")
        if not link or link.startswith("/") or PurePosixPath(link).is_absolute():
            raise UpdaterError("更新包里有越界的符号链接，已取消更新")
        link_pure = PurePosixPath(link)
        if any(part == ".." for part in link_pure.parts):
            raise UpdaterError("更新包里有越界的符号链接，已取消更新")
        if member.issym():
            target = (base / pure.parent / link_pure)
        else:
            target = (base / link_pure)
        if not _is_within(target, base):
            raise UpdaterError("更新包里有越界的符号链接，已取消更新")
    elif not (member.isfile() or member.isdir()):
        raise UpdaterError("更新包里有不支持的文件类型，已取消更新")


def safe_extract(tar_path, dest_dir) -> None:
    """把 tar.gz 解压到 dest_dir，逐个成员做安全检查后再解。"""
    dest = Path(dest_dir)
    try:
        dest.mkdir(parents=True, exist_ok=True)
    except Exception:
        raise UpdaterError("创建临时目录失败（检查磁盘空间）") from None
    base = dest.resolve()
    try:
        tf = tarfile.open(tar_path, "r:gz")
    except Exception:
        raise UpdaterError("更新包解不开（文件可能损坏），稍后再试") from None
    with tf:
        try:
            members = tf.getmembers()
        except Exception:
            raise UpdaterError("更新包解不开（文件可能损坏），稍后再试") from None
        for member in members:
            _check_member(member, base)
        try:
            try:
                tf.extractall(dest, filter="data")
            except TypeError:            # 老版本 Python 没有 filter 参数
                tf.extractall(dest)
        except UpdaterError:
            raise
        except Exception:
            raise UpdaterError("解压更新包失败，稍后再试") from None


def _find_source_root(extract_dir) -> Path:
    """GitHub 源码包解压后是单个顶层目录（如 QQBot-main/）。"""
    d = Path(extract_dir)
    try:
        entries = list(d.iterdir())
    except Exception:
        raise UpdaterError("更新包内容是空的，稍后再试") from None
    if len(entries) == 1 and entries[0].is_dir():
        return entries[0]
    return d


# ---------------------------------------------------------------- 同步文件
def _ignore_bot_path(rel: PurePosixPath) -> bool:
    """bot/ 下这些不动：用户配置、数据、日志、缓存、更新记录。"""
    for part in rel.parts:
        if part in BOT_IGNORE_NAMES or part.startswith(".update-"):
            return True
    name = rel.name
    return name.endswith(".pyc") or name.startswith(".update-")


def _copy_file(src: Path, dst: Path) -> None:
    tmp = dst.with_name(f".{dst.name}.{os.getpid()}.update-tmp")
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, tmp)
        try:
            shutil.copystat(src, tmp)
        except Exception:
            pass
        os.replace(tmp, dst)
    except Exception:
        try:
            tmp.unlink()
        except Exception:
            pass
        raise UpdaterError(f"写入文件失败：{dst.name}（检查磁盘空间和权限）") from None


def _sync_dir(src, dst, ignore=None) -> int:
    """把 src 目录里的文件逐个覆盖到 dst（不删除 dst 里多出来的文件）。"""
    src, dst = Path(src), Path(dst)
    count = 0
    try:
        for cur, dirs, files in os.walk(src):
            dirs.sort()
            cur_path = Path(cur)
            rel_dir = cur_path.relative_to(src)
            for name in sorted(files):
                rel = PurePosixPath((rel_dir / name).as_posix())
                if ignore is not None and ignore(rel):
                    continue
                file_src = cur_path / name
                if not file_src.is_file():
                    continue
                _copy_file(file_src, dst / rel_dir / name)
                count += 1
    except UpdaterError:
        raise
    except Exception:
        raise UpdaterError("写入程序文件失败，检查磁盘空间和权限") from None
    return count


def _sync_tree(src_root) -> dict:
    """同步 bot/、scripts/、start-all.sh；返回各类文件数量。"""
    root = Path(src_root)
    bot_src = root / "bot"
    if not bot_src.is_dir():
        raise UpdaterError("更新包里缺少 bot 目录，已取消更新")
    counts = {"bot": _sync_dir(bot_src, BOT_DIR, _ignore_bot_path)}
    scripts_src = root / "scripts"
    if scripts_src.is_dir():
        counts["scripts"] = _sync_dir(scripts_src, ROOT / "scripts")
    start_src = root / "start-all.sh"
    if start_src.is_file():
        _copy_file(start_src, ROOT / "start-all.sh")
        counts["start-all.sh"] = 1
    return counts


# ---------------------------------------------------------------- 对外接口
def check() -> dict:
    """查最新版本。失败抛 UpdaterError（中文）。"""
    latest = _fetch_latest_commit()
    installed = current_commit()
    behind = (not installed) or installed != latest["commit"]
    return {
        "current": {
            "commit": _short(installed),
            "updated_at": read_meta().get("updated_at"),
        },
        "latest": {
            "commit": _short(latest["commit"]),
            "sha": latest["commit"],
            "title": latest.get("title") or "",
            "url": latest.get("url") or "",
        },
        "behind": bool(behind),
    }


def update() -> bool:
    """后台线程启动更新；已经在更新时返回 False。"""
    global _worker
    with _start_lock:
        if _worker is not None and _worker.is_alive():
            return False
        _set_state(phase="checking", message="正在检查最新版本…", error=None)
        _worker = threading.Thread(target=_update_worker, name="bot-updater", daemon=True)
        try:
            _worker.start()
        except Exception:
            _set_state(phase="failed", message="更新启动失败，请稍后再试",
                       error="更新启动失败，请稍后再试")
            raise UpdaterError("更新启动失败，请稍后再试") from None
        return True


# 兼容不同调用写法
start_update = update


def _update_worker() -> None:
    tmp_dir = None
    try:
        _set_state(phase="checking", message="正在检查最新版本…", error=None)
        latest = _fetch_latest_commit()

        tmp_dir = Path(tempfile.mkdtemp(prefix="qqbot-update-"))
        archive = tmp_dir / f"{BRANCH}.tar.gz"

        _set_state(phase="downloading", message="正在下载更新包…")
        _download(ARCHIVE_URL, archive)

        _set_state(phase="extracting", message="正在解压更新包…")
        extract_dir = tmp_dir / "src"
        safe_extract(archive, extract_dir)
        source_root = _find_source_root(extract_dir)

        _set_state(phase="syncing", message="正在更新程序文件…")
        counts = _sync_tree(source_root)

        _write_meta({
            "version": 1,
            "commit": latest["commit"],
            "title": latest.get("title") or "",
            "updated_at": time.time(),
        })
        total = sum(int(v) for v in counts.values())
        _set_state(
            phase="done",
            message=f"更新完成，共更新 {total} 个文件；重启服务后生效",
            error=None,
            restart_required=True,
        )
    except UpdaterError as e:
        msg = str(e) or "更新失败，请稍后再试"
        _set_state(phase="failed", message=msg, error=msg)
    except Exception as e:                       # 兜底：绝不把英文异常/堆栈给用户
        print(f"[updater] 更新异常: {type(e).__name__}", file=sys.stderr)
        msg = "更新失败，请稍后再试"
        _set_state(phase="failed", message=msg, error=msg)
    finally:
        if tmp_dir is not None:
            shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    # 手动调试：python3 bot/updater.py
    if len(sys.argv) > 1 and sys.argv[1] == "check":
        try:
            print(json.dumps(check(), ensure_ascii=False, indent=2))
        except UpdaterError as e:
            print(f"检查失败：{e}", file=sys.stderr)
            sys.exit(1)
    else:
        print("用法: python3 bot/updater.py check")
