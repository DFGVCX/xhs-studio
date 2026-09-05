"""One worker owns WebDriver: UI requests never access it concurrently."""

from __future__ import annotations

import copy
import hashlib
import queue
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from selenium.common.exceptions import InvalidSessionIdException, NoSuchWindowException

from .browser import BrowserSession, NeedsInteraction
from .config import Settings, load_settings, normalize_urls, note_id
from .storage import ResultStore


ACTIVE = {"opening", "running", "paused", "waiting_login", "stopping"}


class JobStopped(Exception):
    pass


def redact(value):
    return re.sub(r"(?i)(xsec_token|cookie|authorization)([=:\s]+)[^\s&]+", r"\1\2[已隐藏]", str(value))


class JobManager:
    def __init__(self, project_dir: Path, browser_factory=BrowserSession):
        self.project_dir = Path(project_dir)
        self.browser_factory = browser_factory
        self._lock = threading.RLock()
        self._commands = queue.Queue()
        self._actions = queue.Queue(maxsize=50)
        self._shutdown = threading.Event()
        self._stop = threading.Event()
        self._pause = threading.Event()
        self._resume = threading.Event()
        self._remote_barrier = threading.Event()
        self._browser = None
        self._browser_options = None
        self._in_job = False
        self._frame = None
        self._last_frame = 0.0
        self._started = None
        self._finished = None
        self._failed_urls = []
        self._result_sources = {}
        config_error = None
        try:
            self._last_config = load_settings(self.project_dir)
        except ValueError:
            self._last_config = Settings()
            config_error = "配置文件无法读取，已载入默认参数；原文件仍保留，保存参数后可修复。"
        self._state = {
            "status": "idle", "phase": "idle", "message": "配置参数后，打开浏览器登录或开始采集。",
            "browser_open": False, "browser": {}, "counts": self._empty_counts(),
            "started_at": None, "elapsed_seconds": 0, "current_note": "", "logs": [],
            "results": [], "exports": [], "frame_id": 0, "frame_at": None,
            "config": self._last_config.model_dump(), "error": config_error, "retryable_count": 0,
        }
        if config_error:
            self.emit("warning", config_error)
        self._thread = threading.Thread(target=self._worker, name="xhs-browser-owner", daemon=True)
        self._thread.start()

    @staticmethod
    def _empty_counts():
        return dict(discovered=0, processed=0, success=0, partial=0, failed=0, skipped=0, images=0)

    def _update(self, **values):
        with self._lock:
            self._state.update(values)

    def _count(self, **values):
        with self._lock:
            for key, value in values.items():
                self._state["counts"][key] += value

    def emit(self, level, message):
        with self._lock:
            logs = self._state["logs"]
            serial = logs[-1]["id"] + 1 if logs else 1
            logs.append(dict(id=serial, time=datetime.now().isoformat(timespec="seconds"),
                             level=level, message=redact(message)))
            del logs[:-500]
            discovered = re.search(r"搜索已发现\s*(\d+)\s*篇", str(message))
            if discovered and self._state["phase"] == "search":
                self._state["counts"]["discovered"] = int(discovered.group(1))

    def snapshot(self):
        with self._lock:
            result = copy.deepcopy(self._state)
            if self._started is not None:
                result["elapsed_seconds"] = round((self._finished or time.monotonic()) - self._started, 1)
            return result

    def frame(self):
        with self._lock:
            return self._frame

    def remote_snapshot(self):
        """Read the CDP frame cache only; never invoke Selenium on HTTP threads."""
        browser = self._browser
        remote = getattr(browser, "remote", None)
        if remote is None:
            return {"data": None, "sequence": 0, "connected": False, "error": None, "width": 0, "height": 0}
        return {**remote.snapshot(), "transport_id": id(remote)}

    def stream_state(self):
        remote = self.remote_snapshot()
        with self._lock:
            return {
                "type": "state", "status": self._state["status"], "message": self._state["message"],
                "browser_open": self._state["browser_open"], "browser": copy.deepcopy(self._state["browser"]),
                "manual_enabled": self._state["browser_open"] and self._state["status"] not in {"opening", "running", "stopping"} and remote["connected"],
                "stream_available": remote["connected"], "stream_error": remote.get("error"),
            }

    def remote_action(self, action):
        """Manual input has a separate CDP connection and is gated against automation."""
        with self._lock:
            remote = getattr(self._browser, "remote", None)
            if remote is None:
                raise ValueError("浏览器尚未连接，请先打开页内浏览器。")
            if action["type"] != "release" and self._state["status"] in {"running", "opening", "stopping"}:
                raise ValueError("请先点击接管操作，待采集暂停后再操作网页。")
            remote.dispatch(action)

    def release_remote_input(self):
        remote = getattr(self._browser, "remote", None)
        if remote:
            try:
                remote.dispatch({"type": "release"})
            except (ValueError, RuntimeError):
                pass

    def source_url(self, identity):
        with self._lock:
            return self._result_sources.get(identity)

    def _require_free(self):
        if self._state["status"] in ACTIVE:
            raise ValueError("当前任务仍在进行，请先停止任务。")

    def open_browser(self, headless=True, browser="auto", direct_connection=True):
        with self._lock:
            self._require_free()
            self._stop.clear()
            self._update(status="opening", phase="browser", message="正在启动浏览器…", error=None)
            self._commands.put(("open", (headless, browser, direct_connection)))

    def close_browser(self):
        with self._lock:
            self._require_free()
            self._update(status="opening", message="正在关闭浏览器…")
            self._commands.put(("close", None))

    def start(self, config: Settings):
        with self._lock:
            self._require_free()
            if config.mode == "urls" and not config.urls:
                raise ValueError("链接采集模式需要至少一条帖子链接。")
            self._prepare_automation_input()
            self._stop.clear()
            self._pause.clear()
            self._resume.clear()
            self._last_config = config.model_copy(deep=True)
            self._failed_urls = []
            self._result_sources = {}
            self._started = time.monotonic()
            self._finished = None
            self._update(status="running", phase="prepare", message="正在准备任务…", error=None,
                         counts=self._empty_counts(), results=[], exports=[], current_note="", retryable_count=0,
                         started_at=datetime.now(timezone.utc).isoformat(), config=config.model_dump())
            self._commands.put(("run", config.model_copy(deep=True)))

    def pause(self):
        with self._lock:
            if self._state["status"] != "running":
                raise ValueError("只有运行中的任务可以暂停。")
            self._pause.set()
            self._update(message="暂停请求已收到，将在当前操作结束后暂停。")

    def resume(self):
        with self._lock:
            if self._state["status"] not in {"paused", "waiting_login"}:
                raise ValueError("当前没有等待继续的任务。")
            self._prepare_automation_input()
            self._update(status="running", message="正在继续采集…")
            self._pause.clear()
            self._resume.set()

    def stop(self):
        with self._lock:
            if self._state["status"] not in ACTIVE:
                raise ValueError("当前没有正在运行的任务。")
            self._stop.set()
            self._pause.clear()
            self._resume.set()
            self._update(status="stopping", message="正在停止并保存已有结果…")

    def _prepare_automation_input(self):
        # A new browser has no manual inputs to drain. Its first video frame may
        # arrive later than the first automation checkpoint; do not wait on it.
        self._remote_barrier.clear()
        if self.remote_snapshot()["connected"]:
            self.release_remote_input()
            self._remote_barrier.set()

    def retry(self):
        with self._lock:
            self._require_free()
            if not self._failed_urls:
                raise ValueError("没有需要重试的失败链接。")
            config = self._last_config.model_copy(update={"mode": "urls", "urls": list(self._failed_urls)})
            self.start(config)

    def interact(self, action):
        with self._lock:
            if not self._state["browser_open"]:
                raise ValueError("请先打开浏览器。")
            if self._state["status"] in {"running", "opening", "stopping"}:
                raise ValueError("请暂停采集后再手动操作浏览器。")
            try:
                self._actions.put_nowait(action)
            except queue.Full:
                raise ValueError("操作过于频繁，请稍候再试。")

    def _ensure_browser(self, headless, browser, direct_connection=True):
        options = (headless, browser, direct_connection)
        if self._browser is not None:
            try:
                self._browser.info()
            except Exception:
                self.emit("warning", "浏览器连接已中断，正在重新启动。")
                self._close()
        if self._browser is not None and options != self._browser_options:
            self._close()
        if self._browser is None:
            candidate = self.browser_factory(self.project_dir, self.emit, self.checkpoint)
            self._browser = candidate
            try:
                candidate.open(headless=headless, browser=browser, direct_connection=direct_connection)
                self._browser_options = options
                self._update(browser_open=True)
                self._capture(force=True)
            except BaseException:
                self._close()
                raise

    def _close(self):
        self._remote_barrier.clear()
        if self._browser is not None:
            try:
                self._browser.close()
            except Exception as exc:
                self.emit("warning", f"浏览器关闭提示：{exc}")
        self._browser = None
        self._browser_options = None
        with self._lock:
            self._frame = None
            self._state.update(browser_open=False, browser={}, frame_at=None)
        while not self._actions.empty():
            try:
                self._actions.get_nowait()
            except queue.Empty:
                break

    def _capture(self, force=False):
        if self._browser is None or (not force and time.monotonic() - self._last_frame < 0.8):
            return
        self._last_frame = time.monotonic()
        try:
            info = self._browser.info()
            parts = urlsplit(info.get("url", ""))
            info["url"] = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
            stream = self.remote_snapshot()
            # A second CDP session's screenshot can reset Chromium's emulated
            # device scale. While a remote transport exists, only use its cache,
            # including during its initial connection; never compete with it.
            data = stream.get("data") if getattr(self._browser, "remote", None) is not None else self._browser.screenshot()
            with self._lock:
                self._state.update(browser=info)
                if data:
                    self._frame = data
                    self._state.update(frame_id=self._state["frame_id"] + 1,
                                       frame_at=datetime.now(timezone.utc).isoformat())
        except (InvalidSessionIdException, NoSuchWindowException):
            self._close()
            if self._in_job:
                raise RuntimeError("自动化浏览器已关闭，请重新打开后重试。")
            self._update(status="error", message="自动化浏览器已关闭，可点击打开浏览器重新连接。")
        except Exception:
            # Navigation may temporarily make screenshots unavailable; preserve the previous timestamp.
            pass

    def checkpoint(self, seconds=0):
        deadline = time.monotonic() + max(0, seconds)
        while True:
            if self._shutdown.is_set() or self._stop.is_set():
                raise JobStopped()
            if self._remote_barrier.is_set() and self._browser is not None:
                remote = getattr(self._browser, "remote", None)
                if remote is not None and hasattr(remote, "barrier"):
                    if not remote.barrier(timeout=2):
                        raise RuntimeError("浏览器操作尚未完成，请稍后重试。")
                self._remote_barrier.clear()
            while self._browser is not None:
                try:
                    action = self._actions.get_nowait()
                except queue.Empty:
                    break
                try:
                    self._browser.interact(action)
                    self._capture(force=True)
                except Exception as exc:
                    self.emit("warning", f"手动操作未完成：{exc}")
            self._capture()
            if self._pause.is_set() and self._in_job:
                self._update(status="paused", message="任务已暂停，可以手动操作浏览器。")
                time.sleep(0.1)
                continue
            if self._in_job and self._state["status"] == "paused":
                self._update(status="running", message="继续采集…")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(0.1, remaining))

    def _with_interaction(self, operation):
        while True:
            try:
                return operation()
            except NeedsInteraction as exc:
                self._resume.clear()
                self._update(status="waiting_login", message=str(exc))
                self.emit("warning", str(exc))
                self._capture(force=True)
                while not self._resume.is_set():
                    self.checkpoint(0.2)
                self._resume.clear()
                self.checkpoint()
                self._update(status="running", message="验证已提交，正在重新检查页面…")

    def _add_result(self, note):
        result = {key: note.get(key, "") for key in
                  ("note_id", "title", "author", "type", "published_at", "image_count", "status", "url")}
        # Keep signed source links working without showing the token in status JSON.
        source = normalize_urls([result["url"]]) if result.get("url") else []
        with self._lock:
            if source:
                identity = result["note_id"] or hashlib.sha256(source[0].encode()).hexdigest()[:24]
                self._result_sources[identity] = source[0]
                result["url"] = f"/api/notes/{identity}/source"
            self._state["results"].append(result)

    def _add_retry(self, url):
        with self._lock:
            if url not in self._failed_urls:
                self._failed_urls.append(url)
            self._state["retryable_count"] = len(self._failed_urls)

    def _run(self, config):
        self._in_job = True
        store = None
        terminal = "completed"
        try:
            store = ResultStore(self.project_dir, config.keyword, config.output_dir)
            self.emit("info", f"本次内容将保存到：{store.directory}")
            self._ensure_browser(config.headless, config.browser, config.direct_connection)
            self.checkpoint()
            self._update(phase="prepare", message="正在进入小红书首页并检查登录状态…")
            self._with_interaction(lambda: self._browser.prepare_collection(config))
            urls = list(config.urls) if config.mode == "urls" else []
            if config.mode == "search":
                self._update(phase="search", message=f"正在搜索「{config.keyword}」…")
                found = self._with_interaction(lambda: self._browser.search(config))
                urls.extend(found)
            urls = normalize_urls(urls)[:config.max_notes]
            store.save_urls(urls)
            with self._lock:
                self._state["counts"]["discovered"] = len(urls)
            self.emit("info", f"已整理 {len(urls)} 篇待处理笔记。")
            if not urls:
                raise ValueError("没有找到可采集的帖子链接，请检查登录状态、关键词或输入链接。")
            self._update(phase="collect")
            for index, url in enumerate(urls):
                self.checkpoint()
                identity = note_id(url)
                self._update(current_note=identity, message=f"正在处理第 {index + 1} / {len(urls)} 篇笔记")
                if config.skip_existing and store.has(identity, require_images=config.download_images):
                    self._count(skipped=1, processed=1)
                    self.emit("info", f"跳过已保存笔记 {identity}")
                    self._add_result(dict(note_id=identity, title="已保存笔记", status="skipped", url=url, image_count=0))
                    continue
                note = None
                failure = None
                for attempt in range(config.retries + 1):
                    try:
                        note = self._with_interaction(lambda: self._browser.extract(url, config))
                        break
                    except JobStopped:
                        raise
                    except Exception as exc:
                        failure = exc
                        if attempt < config.retries:
                            self.emit("warning", f"笔记 {identity} 读取失败，准备重试 {attempt + 1}/{config.retries}：{exc}")
                            self.checkpoint(config.interval_seconds)
                if note is None:
                    self._add_retry(url)
                    self._count(failed=1, processed=1)
                    self.emit("error", f"笔记 {identity} 失败：{failure}")
                    self._add_result(dict(note_id=identity, title=str(failure), url=url, status="failed", image_count=0))
                else:
                    canonical = note.get("note_id")
                    if config.skip_existing and canonical != identity and store.has(canonical, require_images=config.download_images):
                        self._count(skipped=1, processed=1)
                        self.emit("info", f"分享链接指向已保存笔记 {canonical}，已跳过。")
                        self._add_result(dict(note_id=canonical, title=note.get("title"), status="skipped", url=url, image_count=0))
                        continue
                    self._update(current_note=note.get("title") or identity)
                    try:
                        saved = store.save(note, config, self.emit, self.checkpoint)
                        self._count(success=1, processed=1, images=saved.get("image_count", 0))
                        self._add_result(saved)
                        if saved.get("status") == "partial":
                            self._add_retry(url)
                            self._count(partial=1)
                            self.emit("warning", f"已保存正文，部分内容需补采：{saved.get('title') or identity}")
                        else:
                            self.emit("info", f"已保存：{saved.get('title') or identity}")
                    except JobStopped:
                        saved = next((record for record in store.records() if record["note_id"] == note.get("note_id")), None)
                        if saved:
                            self._count(success=1, processed=1, images=saved.get("image_count", 0),
                                        partial=1 if saved.get("status") == "partial" else 0)
                            self._add_result(saved)
                            if saved.get("status") == "partial":
                                self._add_retry(url)
                        raise
                    except Exception as exc:
                        self._add_retry(url)
                        self._count(failed=1, processed=1)
                        self._add_result(dict(note_id=identity, title=str(exc), url=url, status="failed", image_count=0))
                        self.emit("error", f"笔记 {identity} 保存失败：{exc}")
                if index + 1 < len(urls):
                    self.checkpoint(config.interval_seconds)
        except JobStopped:
            terminal = "stopped"
            self.emit("info", "任务已停止，正在保留已完成的采集结果。")
        except Exception as exc:
            terminal = "error"
            self._update(error=redact(exc))
            self.emit("error", f"任务失败：{exc}")
        finally:
            self._update(phase="export", message="正在生成报告…")
            if store is not None:
                try:
                    self._update(exports=store.export())
                except Exception as exc:
                    terminal = "error"
                    self._update(error=redact(exc))
                    self.emit("error", f"报告导出失败，原始记录仍保存在本地：{exc}")
            self._in_job = False
            self._stop.clear()
            self._pause.clear()
            self._finished = time.monotonic()
            counts = self.snapshot()["counts"]
            message = {"completed": f"采集完成：保存 {counts['success']} 篇（其中 {counts['partial']} 篇待补采），跳过 {counts['skipped']} 篇，失败 {counts['failed']} 篇。",
                       "stopped": "任务已停止，已有结果已保存。", "error": "任务未完成，请查看错误日志。"}[terminal]
            self._update(status=terminal, phase="done", message=message)
            self.emit("info" if terminal != "error" else "error", message)

    def _worker(self):
        try:
            while not self._shutdown.is_set():
                try:
                    command, payload = self._commands.get(timeout=0.15)
                except queue.Empty:
                    if self._browser is not None:
                        try:
                            self.checkpoint()
                        except JobStopped:
                            self._stop.clear()
                    continue
                try:
                    if command == "open":
                        self._ensure_browser(*payload)
                        self.checkpoint()
                        self._update(status="ready", phase="browser",
                                     message="浏览器已连接，可自由访问网页；开始采集时会自动进入小红书。")
                        self.emit("info", "通用浏览器已打开；开始采集后将自动进入小红书，登录信息保存在本机专用目录。")
                    elif command == "close":
                        self._close()
                        self._update(status="idle", phase="idle", message="浏览器已关闭。")
                    elif command == "run":
                        self._run(payload)
                except JobStopped:
                    self._close()
                    self._stop.clear()
                    self._update(status="stopped", message="操作已停止。")
                except Exception as exc:
                    self._update(status="error", message="浏览器操作失败，请查看日志。", error=redact(exc))
                    self.emit("error", str(exc))
        finally:
            self._close()

    def shutdown(self):
        self._shutdown.set()
        self._stop.set()
        self._resume.set()
        self._thread.join(timeout=5)
