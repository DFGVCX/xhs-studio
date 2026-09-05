"""Loopback-only API and static control surface."""

from __future__ import annotations

import asyncio
import json
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .config import Settings, load_settings, resolve_output_root, save_settings
from .browser import validate_navigation_url
from .manager import JobManager


PROJECT_DIR = Path(__file__).resolve().parents[1]


class BrowserOptions(BaseModel):
    headless: bool = True
    browser: Literal["auto", "chrome", "edge"] = "auto"
    direct_connection: bool = True


class FolderOptions(BaseModel):
    current: str = Field(default="Information", min_length=1, max_length=1024)


def choose_output_directory(initial: Path) -> str | None:
    """Show a local native folder picker only after the user clicks the button."""
    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError as exc:
        raise RuntimeError("当前 Python 环境没有可用的本地文件夹选择器，请直接输入绝对路径。") from exc
    candidate = initial
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    root = tk.Tk()
    root.withdraw()
    try:
        root.attributes("-topmost", True)
        root.update_idletasks()
        selected = filedialog.askdirectory(
            parent=root,
            title="选择小红书采集内容保存位置",
            initialdir=str(candidate if candidate.is_dir() else Path.home()),
            mustexist=True,
        )
        return selected or None
    finally:
        root.destroy()


class BrowserAction(BaseModel):
    type: Literal["click", "scroll", "text", "key", "back", "forward", "refresh", "home", "navigate"]
    x: float = Field(default=0.5, ge=0, le=1)
    y: float = Field(default=0.5, ge=0, le=1)
    delta: int = Field(default=0, ge=-3000, le=3000)
    text: str = Field(default="", max_length=2000)
    key: Literal["Enter", "Backspace", "Tab", "Escape", "ArrowDown", "ArrowUp", "Control+a"] = "Enter"
    url: str = Field(default="", max_length=4096)

    @model_validator(mode="after")
    def validate_navigation(self):
        if self.type == "navigate":
            self.url = validate_navigation_url(self.url)
        return self


class RemoteAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["pointer", "wheel", "key", "text", "release", "resize"]
    event: Literal["down", "move", "up"] = "down"
    x: float = Field(default=0.5, ge=0, le=1)
    y: float = Field(default=0.5, ge=0, le=1)
    button: Literal["left", "middle", "right"] = "left"
    buttons: int = Field(default=0, ge=0, le=7)
    click_count: int = Field(default=1, ge=1, le=2)
    delta_x: float = Field(default=0, ge=-3000, le=3000)
    delta_y: float = Field(default=0, ge=-3000, le=3000)
    text: str = Field(default="", max_length=4000)
    key: str = Field(default="", max_length=40)
    code: str = Field(default="", max_length=40)
    modifiers: int = Field(default=0, ge=0, le=15)
    width: int = Field(default=1280, ge=320, le=1920)
    height: int = Field(default=720, ge=240, le=1200)
    quality: Literal["high", "smooth"] = "high"

    @model_validator(mode="after")
    def validate_key(self):
        if self.type == "key" and (not self.key or self.event == "move"):
            raise ValueError("按键需要 key 和 down/up 事件")
        return self


class _ViewportBroker:
    """Let multiple open workbench tabs share one browser without size races.

    Chromium exposes one real viewport. The widest connected viewer owns that
    viewport, while narrower viewers scale the same undistorted frame down.
    """

    def __init__(self, manager):
        self.manager = manager
        self._lock = threading.RLock()
        self._viewports: dict[object, dict] = {}
        self._owner: object | None = None

    @staticmethod
    def _rank(action: dict) -> tuple[int, int, int]:
        width, height = int(action.get("width", 1280)), int(action.get("height", 720))
        return width, width * height, height

    def update(self, viewer: object, action: dict) -> None:
        with self._lock:
            previous_owner = self._owner
            self._viewports[viewer] = dict(action)
            self._owner = max(self._viewports, key=lambda item: self._rank(self._viewports[item]))
            # Reapply when the owning viewer repeats its resize: this is needed
            # after the controlled browser is closed and opened again.
            selected = dict(self._viewports[self._owner]) if self._owner is viewer or self._owner is not previous_owner else None
        if selected is not None:
            self.manager.remote_action(selected)

    def remove(self, viewer: object) -> None:
        with self._lock:
            was_owner = self._owner is viewer
            self._viewports.pop(viewer, None)
            self._owner = max(self._viewports, key=lambda item: self._rank(self._viewports[item])) if self._viewports else None
            selected = dict(self._viewports[self._owner]) if was_owner and self._owner is not None else None
        if selected is not None:
            try:
                self.manager.remote_action(selected)
            except (ValueError, RuntimeError):
                pass


def create_app(project_dir=PROJECT_DIR, manager_factory=JobManager):
    project_dir = Path(project_dir).resolve()

    @asynccontextmanager
    async def lifespan(app):
        app.state.manager = manager_factory(project_dir)
        app.state.viewport_broker = _ViewportBroker(app.state.manager)
        yield
        app.state.manager.shutdown()

    app = FastAPI(title="红薯工作台", version="3.1.0", lifespan=lifespan)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]"])

    @app.middleware("http")
    async def local_requests(request: Request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            if origin and urlsplit(origin).netloc != request.headers.get("host"):
                return JSONResponse({"detail": "只接受来自本工作台的操作。"}, status_code=403)
            if request.headers.get("sec-fetch-site") == "cross-site":
                return JSONResponse({"detail": "跨站操作被拒绝。"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        embedded = request.url.path in {"/browser", "/static/browser.html"}
        response.headers["X-Frame-Options"] = "SAMEORIGIN" if embedded else "DENY"
        response.headers["Content-Security-Policy"] = "frame-ancestors 'self'" if embedded else "frame-ancestors 'none'"
        # This is a local development-style application whose static assets are
        # edited in place. Always revalidate them so a normal refresh cannot mix
        # a new HTML shell with an older cached JavaScript controller.
        if request.url.path == "/" or embedded or request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(ValueError)
    async def invalid_action(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.get("/")
    def index():
        return FileResponse(project_dir / "static" / "index.html")

    @app.get("/browser")
    def browser_view():
        return FileResponse(project_dir / "static" / "browser.html")

    @app.websocket("/api/browser/stream")
    async def browser_stream(socket: WebSocket):
        origin = urlsplit(socket.headers.get("origin", ""))
        expected_scheme = "https" if socket.url.scheme == "wss" else "http"
        if origin.scheme != expected_scheme or origin.netloc != socket.headers.get("host"):
            await socket.close(code=1008)
            return
        await socket.accept()
        manager = socket.app.state.manager
        viewport_broker = socket.app.state.viewport_broker
        viewer = object()
        send_lock = asyncio.Lock()

        async def send_json(value):
            async with send_lock:
                await socket.send_json(value)

        async def frames():
            last_frame = None
            state_at = 0.0
            while True:
                now = asyncio.get_running_loop().time()
                if now - state_at >= 0.3:
                    await send_json(manager.stream_state())
                    state_at = now
                frame = manager.remote_snapshot()
                identity = (frame.get("transport_id"), frame["sequence"])
                if frame.get("data") and frame.get("connected") and identity != last_frame:
                    async with send_lock:
                        await socket.send_bytes(frame["data"])
                    last_frame = identity
                await asyncio.sleep(0.04)

        async def inputs():
            while True:
                raw = await socket.receive_text()
                if len(raw) > 24000:
                    await send_json({"type": "error", "message": "输入内容过长，请分段粘贴。"})
                    continue
                try:
                    packet = json.loads(raw)
                    if not isinstance(packet, dict) or packet.get("type") != "input":
                        raise ValueError("不支持的浏览器消息")
                    action = RemoteAction.model_validate(packet.get("action"))
                    normalized = action.model_dump(exclude_unset=True)
                    if action.type == "resize":
                        viewport_broker.update(viewer, normalized)
                    else:
                        manager.remote_action(normalized)
                    await send_json({"type": "ack"})
                except (ValidationError, ValueError, RuntimeError) as exc:
                    message = "浏览器输入参数无效。" if isinstance(exc, ValidationError) else str(exc)
                    await send_json({"type": "error", "message": message})

        tasks = [asyncio.create_task(frames()), asyncio.create_task(inputs())]
        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                try:
                    task.result()
                except (WebSocketDisconnect, RuntimeError):
                    pass
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            viewport_broker.remove(viewer)
            manager.release_remote_input()

    @app.get("/api/config")
    def config():
        try:
            return load_settings(project_dir)
        except ValueError:
            return Settings()

    @app.put("/api/config")
    def update_config(settings: Settings):
        save_settings(project_dir, settings)
        return settings

    @app.get("/api/state")
    def state(request: Request):
        return request.app.state.manager.snapshot()

    @app.get("/api/frame.jpg")
    def frame(request: Request):
        data = request.app.state.manager.frame()
        return Response(content=data, media_type="image/jpeg") if data else Response(status_code=204)

    @app.get("/api/notes/{identity}/source")
    def source(identity: str, request: Request):
        url = request.app.state.manager.source_url(identity)
        if not url:
            raise HTTPException(404, "当前任务中没有此笔记，请从导出报告查看原始链接。")
        return RedirectResponse(url)

    @app.post("/api/browser/open")
    def open_browser(options: BrowserOptions, request: Request):
        request.app.state.manager.open_browser(options.headless, options.browser, options.direct_connection)
        return {"accepted": True}

    @app.post("/api/browser/close")
    def close_browser(request: Request):
        request.app.state.manager.close_browser()
        return {"accepted": True}

    @app.post("/api/folders/select")
    def select_folder(options: FolderOptions):
        try:
            initial = resolve_output_root(project_dir, Settings(output_dir=options.current).output_dir)
            selected = choose_output_directory(initial)
            if not selected:
                return {"cancelled": True, "path": None}
            validated = Settings(output_dir=selected).output_dir
            return {"cancelled": False, "path": validated}
        except (OSError, RuntimeError, ValueError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/api/browser/action")
    def action(body: BrowserAction, request: Request):
        request.app.state.manager.interact(body.model_dump())
        return {"accepted": True}

    @app.post("/api/jobs/start")
    def start(settings: Settings, request: Request):
        request.app.state.manager.start(settings)
        save_settings(project_dir, settings)
        return {"accepted": True}

    @app.post("/api/jobs/{operation}")
    def control(operation: Literal["pause", "resume", "stop", "retry"], request: Request):
        getattr(request.app.state.manager, operation)()
        return {"accepted": True}

    @app.get("/api/files/{relative_path:path}")
    def download(relative_path: str, request: Request):
        current = request.app.state.manager.snapshot().get("config", {})
        try:
            root = resolve_output_root(project_dir, current.get("output_dir", "Information"))
        except ValueError as exc:
            raise HTTPException(404, "当前任务的保存路径不可用。") from exc
        path = (root / relative_path).resolve()
        if not path.is_relative_to(root) or not path.is_file() or "console" not in path.relative_to(root).parts:
            raise HTTPException(404, "文件不存在。")
        # Browser login profiles and source files are never served.
        return FileResponse(path, filename=path.name)

    app.mount("/static", StaticFiles(directory=project_dir / "static", check_dir=False), name="static")
    return app


app = create_app()
