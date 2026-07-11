"""Process configuration shared by the Codex-native backend.

Only deployment and local filesystem controls belong here.  Model selection,
MCP configuration, Skills, and approvals are owned by ``codex app-server``.
"""

from __future__ import annotations

import os
import secrets
import shutil
import sys
from pathlib import Path

from dotenv import load_dotenv


def env_int(name: str, default: int, *, min_value: int | None = None) -> int:
    """Read a bounded integer environment value without breaking startup."""
    raw = os.environ.get(name)
    if raw is None or raw == "":
        value = default
    else:
        try:
            value = int(raw)
        except ValueError:
            print(
                f"[muselab] {name}={raw!r} is not an integer; falling back to {default}",
                file=sys.stderr,
                flush=True,
            )
            value = default
    return max(value, min_value) if min_value is not None else value


def env_float(name: str, default: float) -> float:
    """Read a float environment value without breaking startup."""
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        print(
            f"[muselab] {name}={raw!r} is not a number; falling back to {default}",
            file=sys.stderr,
            flush=True,
        )
        return default


def locate_executable(name: str) -> str | None:
    """Find a CLI binary when a user service has a minimal PATH."""
    found = shutil.which(name)
    if found:
        return found
    home = Path.home()
    candidates: list[Path] = [
        home / ".local" / "bin",
        home / ".cargo" / "bin",
        home / ".npm-global" / "bin",
        home / "bin",
        Path("/usr/local/bin"),
        Path("/opt/homebrew/bin"),
        Path("/usr/bin"),
    ]
    if name in {"codex", "node", "npm"}:
        nvm_node = home / ".nvm" / "versions" / "node"
        if nvm_node.exists():
            candidates[:0] = [
                version / "bin"
                for version in sorted(nvm_node.iterdir(), reverse=True)
                if version.is_dir()
            ]
        candidates.insert(0, home / ".volta" / "bin")
    for directory in candidates:
        executable = directory / name
        if executable.exists() and os.access(executable, os.X_OK):
            return str(executable)
    return None


def atomic_write_text(path: Path, data: str, encoding: str = "utf-8") -> None:
    """Atomically replace a text file, including the directory rename sync."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f"{path.name}.tmp.{os.getpid()}.{secrets.token_hex(4)}")
    try:
        with temporary.open("w", encoding=encoding) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


load_dotenv(Path(__file__).resolve().parent.parent / ".env")

_root_value = os.environ.get("MUSELAB_ROOT", "")
_raw_root = Path(_root_value) if _root_value else None
ROOT = _raw_root.resolve() if _raw_root else None
TOKEN = os.environ.get("MUSELAB_TOKEN", "")
PORT = env_int("MUSELAB_PORT", 8765, min_value=1)
HOST = os.environ.get("MUSELAB_HOST", "127.0.0.1")
CODEX_BIN = os.environ.get("CODEX_BIN") or locate_executable("codex") or "codex"
CODEX_HISTORY_READ_TIMEOUT = env_float(
    "MUSELAB_CODEX_HISTORY_READ_TIMEOUT_SECONDS", 8.0)
CODEX_COMPACT_TIMEOUT = env_float("MUSELAB_CODEX_COMPACT_TIMEOUT_SECONDS", 600.0)

if not TOKEN:
    raise RuntimeError("MUSELAB_TOKEN must be set in .env")
if len(TOKEN) < 16:
    raise RuntimeError("MUSELAB_TOKEN too short (need >=16 chars)")
if ROOT is None:
    raise RuntimeError("MUSELAB_ROOT must be set in .env (do NOT default to $HOME)")
if not ROOT.exists():
    raise RuntimeError(f"MUSELAB_ROOT does not exist: {ROOT}")

_FORBIDDEN_ROOTS = {
    Path("/"), Path("/etc"), Path("/root"), Path("/home"), Path("/var"),
    Path("/usr"), Path("/boot"),
}
_forbidden_resolved = set(_FORBIDDEN_ROOTS)
for forbidden in _FORBIDDEN_ROOTS:
    try:
        _forbidden_resolved.add(forbidden.resolve())
    except OSError:
        pass
if ROOT in _forbidden_resolved or _raw_root in _FORBIDDEN_ROOTS:
    raise RuntimeError(
        f"MUSELAB_ROOT={ROOT} is a system / cross-user path. Point it at "
        "your $HOME or a sub-directory you own."
    )
