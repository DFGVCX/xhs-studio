"""Validated console settings and conservative import of the original script config."""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qs, urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


_INVALID_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
_RESERVED_FILENAME = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", re.I)
_NOTE_PATH = re.compile(r"^/(?:explore|search_result|discovery/item)/([0-9a-fA-F]{24})/?$")
DEFAULT_OUTPUT_DIR = "Information"


def clean_filename(value: str, max_length: int = 80) -> str:
    """Produce a Windows-safe leaf name, never a path."""
    text = _INVALID_FILENAME.sub("_", str(value))
    text = re.sub(r"\s+", " ", text).strip(" .")[:max_length].rstrip(" .")
    if not text or text in {".", ".."}:
        return "未命名"
    if _RESERVED_FILENAME.match(text):
        text = "_" + text
    return text


def _keyword(value: str) -> str:
    value = value.strip()
    if not value or len(value) > 80:
        raise ValueError("关键词长度应为 1～80 个字符")
    if (
        _INVALID_FILENAME.search(value)
        or ".." in value
        or value.endswith((".", " "))
        or _RESERVED_FILENAME.match(value)
    ):
        raise ValueError("关键词不能包含路径、Windows 保留名称或文件名非法字符")
    return value


def _output_dir(value: str) -> str:
    """Validate a local output root without expanding variables or network paths."""
    text = str(value).strip()
    if not text or len(text) > 1024 or any(ord(character) < 32 for character in text):
        raise ValueError("保存路径不能为空、包含控制字符或超过 1024 个字符")
    if text.startswith(("\\\\", "//", "\\\\?\\", "\\\\.\\")) or text.startswith("~"):
        raise ValueError("保存路径仅支持本机磁盘或项目内相对目录，不支持网络、设备或用户缩写路径")
    path = Path(text)
    if not path.is_absolute() and any(part == ".." for part in path.parts):
        raise ValueError("相对保存路径不能离开项目目录")
    invalid_parts = path.parts[1:] if path.drive else path.parts
    if any(re.search(r'[<>:"|?*]', part) for part in invalid_parts):
        raise ValueError("保存路径包含 Windows 不支持的字符")
    return str(path)


def resolve_output_root(project_dir: Path | str, output_dir: str = DEFAULT_OUTPUT_DIR) -> Path:
    """Resolve an approved output root; relative paths remain inside the project."""
    root = Path(project_dir).resolve()
    configured = Path(_output_dir(output_dir))
    resolved = configured.resolve() if configured.is_absolute() else (root / configured).resolve()
    if not configured.is_absolute() and not resolved.is_relative_to(root):
        raise ValueError("相对保存路径不能离开项目目录")
    if resolved.exists() and not resolved.is_dir():
        raise ValueError("保存路径指向文件，请选择文件夹")
    return resolved


def _validated_url(value: str) -> str:
    value = value.strip()
    if len(value) > 4096 or any(ord(char) <= 32 for char in value) or any(char in value for char in '\\<>"'):
        raise ValueError("笔记链接包含非法字符或长度超过限制")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("笔记链接格式错误") from exc
    if parsed.username or parsed.password or port is not None or parsed.fragment:
        raise ValueError("笔记链接不能包含账号、端口或片段标记")
    host = (parsed.hostname or "").lower()
    if host == "www.xiaohongshu.com":
        valid = parsed.scheme == "https" and bool(_NOTE_PATH.fullmatch(parsed.path))
    elif host == "xhslink.com":
        valid = parsed.scheme in {"http", "https"} and bool(
            re.fullmatch(r"/(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_-]+/?", parsed.path)
        )
    else:
        valid = False
    if not valid:
        raise ValueError("仅支持小红书 explore/search_result/discovery/item 详情链接或 xhslink.com 分享短链")
    # The original signed query must not be decoded, reordered or reconstructed.
    return value


def note_id(url: str) -> str:
    """Return a canonical note ID; short links remain unresolved until navigation."""
    try:
        parsed = urlsplit(_validated_url(url))
    except (ValueError, TypeError, AttributeError):
        return ""
    match = _NOTE_PATH.fullmatch(parsed.path)
    return match.group(1).lower() if parsed.hostname == "www.xiaohongshu.com" and match else ""


def normalize_urls(urls: list[str]) -> list[str]:
    """Validate and deduplicate while preserving input order and URL signatures."""
    if len(urls) > 1000:
        raise ValueError("单次最多输入 1000 条笔记链接")
    result: list[str] = []
    positions: dict[str, int] = {}
    for candidate in urls:
        if not isinstance(candidate, str):
            raise ValueError("每条笔记链接都必须是文本")
        if not candidate.strip():
            continue
        url = _validated_url(candidate)
        identity = note_id(url) or url
        if identity not in positions:
            positions[identity] = len(result)
            result.append(url)
        else:
            index = positions[identity]
            # A later signed link may carry a refreshed token; never replace it
            # with an unsigned URL while preserving the original discovery order.
            if parse_qs(urlsplit(url).query).get("xsec_token"):
                result[index] = url
    return result


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    keyword: str = "Agent面经"
    mode: Literal["search", "urls"] = "search"
    urls: list[str] = Field(default_factory=list, max_length=1000)
    search_seconds: int = Field(default=60, ge=1, le=3600)
    max_notes: int = Field(default=20, ge=1, le=500)
    interval_seconds: float = Field(default=3, ge=1, le=60, allow_inf_nan=False)
    page_timeout: int = Field(default=30, ge=5, le=120)
    retries: int = Field(default=1, ge=0, le=3)
    download_images: bool = True
    skip_existing: bool = True
    headless: bool = True
    direct_connection: bool = True
    browser: Literal["auto", "chrome", "edge"] = "auto"
    naming: Literal["title", "id", "content"] = "title"
    output_dir: str = DEFAULT_OUTPUT_DIR

    @field_validator("keyword")
    @classmethod
    def validate_keyword(cls, value: str) -> str:
        return _keyword(value)

    @field_validator("urls")
    @classmethod
    def validate_urls(cls, value: list[str]) -> list[str]:
        return normalize_urls(value)

    @field_validator("output_dir")
    @classmethod
    def validate_output_dir(cls, value: str) -> str:
        return _output_dir(value)

    @model_validator(mode="after")
    def validate_mode(self) -> "Settings":
        if self.mode == "urls" and not self.urls:
            raise ValueError("链接采集模式至少需要一条有效笔记链接")
        return self


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix="." + path.name, suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load_settings(project_dir: Path | str) -> Settings:
    """Prefer the console config; import legacy parameters without rewriting them."""
    root = Path(project_dir)
    path = root / "runtime" / "config.json"
    if path.exists():
        try:
            return Settings.model_validate_json(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            raise ValueError(f"无法读取控制台配置 {path.name}：{exc}") from exc

    legacy = root / "parameter.txt"
    if not legacy.exists():
        return Settings()
    try:
        lines = legacy.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeError):
        return Settings()
    lines = [line.strip() for line in lines if line.strip()]
    config: dict = {}
    urls: list[str] = []
    for line in lines:
        if line.startswith(("https://", "http://")):
            try:
                urls.extend(normalize_urls([line]))
            except ValueError:
                continue
    if lines and "|" in lines[-1]:
        parts = lines[-1].split("|")
        try:
            config["keyword"] = _keyword(parts[0])
        except ValueError:
            pass

        def bounded(index: int, fallback: float, lower: float, upper: float) -> float:
            try:
                value = float(parts[index])
                return min(upper, max(lower, value)) if math.isfinite(value) else fallback
            except (ValueError, IndexError):
                return fallback

        config["search_seconds"] = int(bounded(1, 60, 1, 3600))
        config["interval_seconds"] = bounded(2, 3, 1, 60)
        config["mode"] = "urls" if len(parts) > 3 and parts[3] == "0" and urls else "search"
        config["naming"] = {"0": "title", "1": "id", "2": "content"}.get(
            parts[4] if len(parts) > 4 else "0", "title"
        )
    config["urls"] = normalize_urls(urls[:1000])
    # The embedded browser provides login and direct input without another window.
    config["headless"] = True
    # Keep collection traffic independent from ambient HTTP/PAC proxy settings.
    config["direct_connection"] = True
    return Settings.model_validate(config)


def save_settings(project_dir: Path | str, settings: Settings) -> None:
    validated = Settings.model_validate(settings.model_dump())
    _atomic_json(Path(project_dir) / "runtime" / "config.json", validated.model_dump())
