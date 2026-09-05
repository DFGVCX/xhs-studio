"""Resumable note storage, bounded media downloads, and regenerated exports."""

from __future__ import annotations

import copy
import csv
import hashlib
import ipaddress
import json
import os
import re
import socket
import tempfile
import threading
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import quote, urljoin, urlsplit

import requests
from PIL import Image, UnidentifiedImageError

from .config import Settings, _atomic_json, _keyword, clean_filename, normalize_urls, note_id, resolve_output_root


MAX_MEDIA_BYTES = 20 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
MAX_IMAGES_PER_NOTE = 50
MAX_CONTENT_CHARS = 100_000
_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0 Safari/537.36"


def _public_media_url(url: str) -> str:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("图片 URL 端口无效") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.username
        or parsed.password
        or port is not None
        or len(url) > 8192
        or "\\" in url
        or any(ord(char) < 32 for char in url)
        or not any(host == suffix or host.endswith("." + suffix) for suffix in ("xhscdn.com", "xiaohongshu.com"))
    ):
        raise ValueError("图片地址必须属于公开的小红书 CDN")
    try:
        addresses = socket.getaddrinfo(host, 443 if parsed.scheme == "https" else 80, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError("无法解析图片服务器") from exc
    if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise ValueError("拒绝访问非公网图片地址")
    return url


def _download_image(url: str, destination: Path, config: Settings, checkpoint: Callable) -> None:
    """Download only bounded CDN images; validate each redirect before following it."""
    descriptor, temporary = tempfile.mkstemp(prefix=".download-", dir=destination.parent)
    os.close(descriptor)
    temporary_path = Path(temporary)
    png_path = temporary_path.with_suffix(".png")
    try:
        with requests.Session() as session:
            # Do not forward browser cookies, netrc credentials or ambient proxy auth.
            session.trust_env = False
            current = url
            for _redirect in range(4):
                checkpoint()
                _public_media_url(current)
                with session.get(
                    current,
                    headers={"Referer": "https://www.xiaohongshu.com/", "User-Agent": _USER_AGENT},
                    timeout=(min(10, config.page_timeout), config.page_timeout),
                    stream=True,
                    allow_redirects=False,
                ) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("Location")
                        if not location:
                            raise ValueError("图片重定向缺少目标地址")
                        current = urljoin(current, location)
                        continue
                    response.raise_for_status()
                    if 300 <= response.status_code < 400:
                        raise ValueError("不支持的图片重定向")
                    content_length = response.headers.get("Content-Length", "")
                    if content_length.isdigit() and int(content_length) > MAX_MEDIA_BYTES:
                        raise ValueError("图片超过 20 MB 下载限制")
                    downloaded = 0
                    with temporary_path.open("wb") as stream:
                        for chunk in response.iter_content(chunk_size=64 * 1024):
                            checkpoint()
                            if chunk:
                                downloaded += len(chunk)
                                if downloaded > MAX_MEDIA_BYTES:
                                    raise ValueError("图片超过 20 MB 下载限制")
                                stream.write(chunk)
                    break
            else:
                raise ValueError("图片重定向次数过多")
        try:
            with Image.open(temporary_path) as picture:
                if picture.width * picture.height > MAX_IMAGE_PIXELS:
                    raise ValueError("图片像素数量超过限制")
                picture.verify()
            with Image.open(temporary_path) as picture:
                # Saving all frames keeps animated source media intact as APNG.
                if getattr(picture, "is_animated", False):
                    frame_count = getattr(picture, "n_frames", 1)
                    if picture.width * picture.height * frame_count > MAX_IMAGE_PIXELS:
                        raise ValueError("动画图片总像素数量超过限制")
                    frames = []
                    durations = []
                    for index in range(frame_count):
                        checkpoint()
                        picture.seek(index)
                        frames.append(picture.convert("RGBA"))
                        durations.append(picture.info.get("duration", 100))
                    frames[0].save(png_path, "PNG", save_all=True, append_images=frames[1:], duration=durations, loop=picture.info.get("loop", 0))
                else:
                    if picture.mode not in {"RGB", "RGBA", "L", "LA", "P", "I", "I;16"}:
                        picture = picture.convert("RGBA" if "A" in picture.getbands() else "RGB")
                    picture.save(png_path, "PNG")
            os.replace(png_path, destination)
        except (UnidentifiedImageError, Image.DecompressionBombError, OSError) as exc:
            raise ValueError("下载内容不是有效图片，或图片文件已损坏") from exc
    finally:
        temporary_path.unlink(missing_ok=True)
        png_path.unlink(missing_ok=True)


def _text(value: object, limit: int = 1000) -> str:
    return str(value if value is not None else "").replace("\x00", "")[:limit].strip()


def _markdown(value: object) -> str:
    text = str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return re.sub(r"([\\`*_{}\[\]()#+\-!|])", r"\\\1", text)


def _csv_cell(value: object) -> str:
    text = str(value if value is not None else "")
    # Spreadsheet software also recognizes formulas after leading whitespace.
    return "'" + text if text.lstrip().startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r", "\n")) else text


def _atomic_text(path: Path, writer: Callable, encoding: str = "utf-8") -> None:
    descriptor, temporary = tempfile.mkstemp(prefix="." + path.name, suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding=encoding, newline="") as stream:
            writer(stream)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class ResultStore:
    """One collection per keyword, with an upsert index keyed by the stable note ID."""

    def __init__(self, project_dir: Path | str, keyword: str, output_dir: str = "Information"):
        self.project_dir = Path(project_dir).resolve()
        self.keyword = _keyword(keyword)
        self.output_root = resolve_output_root(self.project_dir, output_dir)
        self.directory = self.output_root / self.keyword / "console"
        self.directory.mkdir(parents=True, exist_ok=True)
        (self.directory / "Images").mkdir(exist_ok=True)
        default_root = resolve_output_root(self.project_dir)
        if self.output_root == default_root:
            self._path = self.project_dir / "runtime" / "collections" / self.keyword / "notes.json"
        else:
            root_key = hashlib.sha256(os.path.normcase(str(self.output_root)).encode("utf-8")).hexdigest()[:16]
            self._path = self.project_dir / "runtime" / "collections" / "by-output" / root_key / self.keyword / "notes.json"
        self._lock = threading.RLock()
        self._notes: dict[str, dict] = {}
        if self._path.exists():
            try:
                if self._path.stat().st_size > 64 * 1024 * 1024:
                    raise ValueError("结果文件超过 64 MB 读取限制")
                records = json.loads(self._path.read_text(encoding="utf-8"))
                if not isinstance(records, list) or any(not isinstance(record, dict) or not record.get("note_id") for record in records):
                    raise ValueError("结果文件结构无效")
                self._notes = {str(record["note_id"]): record for record in records}
            except (OSError, ValueError) as exc:
                raise ValueError(f"已有采集结果无法读取，已保留原文件：{self._path}") from exc

    def has(self, identity: str, require_images: bool = False) -> bool:
        with self._lock:
            record = self._notes.get(identity)
            # Retry incomplete records so a failed image does not get skipped forever.
            if not record or record.get("status") != "success":
                return False
            if require_images:
                local_images = record.get("local_images", [])
                if len(local_images) != len(record.get("images", [])):
                    return False
                for relative in local_images:
                    path = (self.directory / relative).resolve()
                    if not path.is_relative_to(self.directory.resolve()) or not path.is_file():
                        return False
            return True

    def records(self) -> list[dict]:
        with self._lock:
            return copy.deepcopy(list(self._notes.values()))

    def _commit(self, record: dict) -> None:
        with self._lock:
            candidate = dict(self._notes)
            candidate[record["note_id"]] = copy.deepcopy(record)
            _atomic_json(self._path, list(candidate.values()))
            self._notes = candidate

    def save_urls(self, urls: list[str]) -> None:
        validated = normalize_urls(urls)
        with self._lock:
            _atomic_text(self.directory / "links.txt", lambda stream: stream.write("\n".join(validated) + ("\n" if validated else "")))

    def save(
        self,
        note: dict,
        config: Settings,
        emit: Callable[[str, str], None],
        checkpoint: Callable[[float], None] = lambda seconds=0: None,
        cookies: list | None = None,
    ) -> dict:
        del cookies  # Browser credentials are never forwarded to image servers.
        raw_url = _text(note.get("url"), 4096)
        urls = normalize_urls([raw_url])
        if not urls:
            raise ValueError("笔记缺少有效详情页链接")
        identity = note_id(raw_url) or _text(note.get("note_id"), 80).lower()
        if not re.fullmatch(r"[0-9a-f]{24}", identity):
            raise ValueError("无法确定笔记 ID，请先解析分享短链")
        images = note.get("images") or []
        if not isinstance(images, list):
            raise ValueError("笔记图片字段必须是列表")
        if len(images) > MAX_IMAGES_PER_NOTE:
            raise ValueError("单篇笔记图片数量超过 50 张限制")
        image_urls = list(dict.fromkeys(_text(url, 8192) for url in images if isinstance(url, str) and url.strip()))
        title = _text(note.get("title"), 500)
        content = _text(note.get("content"), MAX_CONTENT_CHARS)
        errors: list[str] = []
        if not title and not content and not image_urls:
            errors.append("未提取到标题、正文或图片，请确认页面是否可访问")
        record = {
            "note_id": identity,
            "url": raw_url,
            "title": title or "未命名笔记",
            "author": _text(note.get("author"), 200) or "未知作者",
            "content": content,
            "published_at": _text(note.get("published_at"), 100),
            "location": _text(note.get("location"), 200),
            "type": _text(note.get("type"), 30) or "image",
            "images": image_urls,
            "local_images": [],
            "image_count": 0,
            "source": _text(note.get("source"), 500),
            "collected_at": datetime.now(timezone.utc).isoformat(),
            "status": "success",
            "errors": errors,
        }
        naming_text = content if config.naming == "content" else title
        basename = identity if config.naming == "id" else identity + "_" + clean_filename(naming_text or "未命名笔记", 48)

        def persist_progress(pending: int = 0) -> None:
            progress = copy.deepcopy(record)
            progress["image_count"] = len(progress["local_images"])
            if pending:
                progress["errors"].append(f"尚有 {pending} 张图片未完成下载，可重新运行补采")
            progress["status"] = "partial" if progress["errors"] else "success"
            self._commit(progress)

        # Keep extracted text even if the user stops during the first image request.
        persist_progress(len(image_urls) if config.download_images else 0)
        if config.download_images:
            for index, url in enumerate(image_urls, start=1):
                checkpoint()
                # URL hash prevents accidental reuse if image order/source changed.
                signature = hashlib.sha256(url.encode("utf-8")).hexdigest()[:8]
                destination = self.directory / "Images" / f"{basename}_{index:02d}_{signature}.png"
                failure = ""
                for attempt in range(config.retries + 1):
                    try:
                        if destination.is_file():
                            try:
                                with Image.open(destination) as existing:
                                    existing.verify()
                            except (OSError, ValueError):
                                destination.unlink()
                        if not destination.is_file():
                            _download_image(url, destination, config, checkpoint)
                        record["local_images"].append(destination.relative_to(self.directory).as_posix())
                        break
                    except (requests.RequestException, OSError, ValueError) as exc:
                        # Checkpoint cancellation exceptions intentionally propagate.
                        failure = f"图片 {index} 下载失败：{str(exc)[:250]}"
                        if attempt < config.retries:
                            emit("warning", f"{record['title']}：图片 {index} 重试 {attempt + 1}/{config.retries}")
                            checkpoint(min(2 ** attempt, 4))
                else:
                    errors.append(failure)
                    emit("warning", failure)
                persist_progress(len(image_urls) - index)
        record["image_count"] = len(record["local_images"])
        record["status"] = "partial" if errors else "success"
        self._commit(record)
        return copy.deepcopy(record)

    def export(self) -> list[dict[str, str]]:
        """Replace reports from all records; reruns never append duplicate Markdown."""
        with self._lock:
            notes = list(self._notes.values())
            prefix = clean_filename(self.keyword)
            json_path = self.directory / f"{prefix}.json"
            csv_path = self.directory / f"{prefix}.csv"
            md_path = self.directory / f"{prefix}.md"
            text_path = self.directory / f"{prefix}【纯文字版】.md"
            zip_path = self.directory / f"{prefix}.zip"
            _atomic_json(json_path, notes)

            def csv_writer(stream):
                columns = ["note_id", "title", "author", "content", "published_at", "location", "type", "url", "image_count", "status", "errors"]
                writer = csv.writer(stream)
                writer.writerow(columns)
                for record in notes:
                    writer.writerow([_csv_cell("; ".join(record.get("errors", [])) if column == "errors" else record.get(column, "")) for column in columns])

            _atomic_text(csv_path, csv_writer, encoding="utf-8-sig")

            def markdown_writer(stream, include_images: bool):
                stream.write(f"# {_markdown(self.keyword)}\n\n共 {len(notes)} 篇笔记。\n\n")
                for record in notes:
                    stream.write(f"## {_markdown(record['title'])}\n\n")
                    stream.write(f"作者：{_markdown(record.get('author', ''))}  \n时间：{_markdown(record.get('published_at', '') or '未提供')}  \n地点：{_markdown(record.get('location', '') or '未提供')}  \n")
                    stream.write(f"来源：[查看原笔记](<{record['url']}>)\n\n")
                    stream.write(_markdown(record.get("content", "")).replace("\n", "  \n") + "\n\n")
                    if include_images:
                        for index, relative in enumerate(record.get("local_images", []), start=1):
                            stream.write(f"![图片 {index}]({quote(relative, safe='/')})\n\n")
                    if record.get("errors"):
                        stream.write("采集提示：" + _markdown("；".join(record["errors"])) + "\n\n")
                    stream.write("---\n\n")

            _atomic_text(md_path, lambda stream: markdown_writer(stream, True))
            _atomic_text(text_path, lambda stream: markdown_writer(stream, False))
            descriptor, temporary = tempfile.mkstemp(prefix=".export-", suffix=".zip", dir=self.directory)
            os.close(descriptor)
            try:
                with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                    for path in (json_path, csv_path, md_path, text_path, self.directory / "links.txt"):
                        if path.exists():
                            archive.write(path, path.name)
                    included: set[str] = set()
                    for record in notes:
                        for relative in record.get("local_images", []):
                            path = (self.directory / relative).resolve()
                            if path.is_relative_to(self.directory.resolve()) and path.is_file() and relative not in included:
                                archive.write(path, Path(relative).as_posix())
                                included.add(relative)
                os.replace(temporary, zip_path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            result = []
            for path in (md_path, text_path, json_path, csv_path, zip_path, self.directory / "links.txt"):
                if path.exists():
                    relative = path.relative_to(self.output_root).as_posix()
                    result.append({"name": path.name, "url": "/api/files/" + quote(relative, safe="/")})
            return result
