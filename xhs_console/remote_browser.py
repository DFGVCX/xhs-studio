"""Live Chromium viewport and native input, independent of Selenium's worker.

Only a loopback DevTools endpoint supplied by our WebDriver is accepted. A single
background thread owns the CDP socket, acknowledges frames immediately, and sends
input in order. It never reads a personal browser profile or calls Selenium.
"""

from __future__ import annotations

import base64
import http.client
import ipaddress
import json
import math
import socket
import threading
import time
from collections import deque
from typing import Callable
from urllib.parse import urlsplit

import websocket


_BUTTONS = {"left": 1, "right": 2, "middle": 4}
_KEY_CODES = {
    "Backspace": 8, "Tab": 9, "Enter": 13, "Shift": 16, "Control": 17,
    "Alt": 18, "Pause": 19, "CapsLock": 20, "Escape": 27, " ": 32,
    "PageUp": 33, "PageDown": 34, "End": 35, "Home": 36,
    "ArrowLeft": 37, "ArrowUp": 38, "ArrowRight": 39, "ArrowDown": 40,
    "Insert": 45, "Delete": 46, "Meta": 91,
}


class _CaptureInterrupted(Exception):
    """A new document invalidated Chromium's pending screenshot callback."""


def _loopback_host(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _number(value, name: str, lower: float, upper: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"无效的 {name}") from exc
    if not math.isfinite(number):
        raise ValueError(f"无效的 {name}")
    return max(lower, min(upper, number))


def normalize_action(action: dict) -> dict:
    """Validate the public input protocol without touching a browser/socket."""
    if not isinstance(action, dict):
        raise ValueError("浏览器操作必须是对象")
    kind = action.get("type")
    if kind == "release":
        return {"type": "release"}
    if kind == "resize":
        quality = action.get("quality", "high")
        if not isinstance(quality, str) or quality not in {"high", "smooth"}:
            raise ValueError("画质只能选择 high 或 smooth")
        return {"type": kind,
                "width": int(_number(action.get("width"), "width", 320, 1920)),
                "height": int(_number(action.get("height"), "height", 240, 1200)),
                "quality": quality}
    if kind == "text":
        value = action.get("text", "")
        if not isinstance(value, str) or len(value) > 4000:
            raise ValueError("单次输入最多 4000 个字符")
        return {"type": kind, "text": value}
    if kind in {"pointer", "wheel"}:
        result = {"type": kind,
                  "x": _number(action.get("x", .5), "x", 0, 1),
                  "y": _number(action.get("y", .5), "y", 0, 1),
                  "modifiers": int(_number(action.get("modifiers", 0), "modifiers", 0, 15))}
        if kind == "wheel":
            result.update(delta_x=_number(action.get("delta_x", 0), "delta_x", -3000, 3000),
                          delta_y=_number(action.get("delta_y", 0), "delta_y", -3000, 3000))
        else:
            event = action.get("event")
            button = action.get("button", "left")
            if event not in {"down", "move", "up"} or button not in _BUTTONS:
                raise ValueError("无效的指针事件")
            result.update(event=event, button=button,
                          buttons=int(_number(action.get("buttons", 0), "buttons", 0, 7)),
                          click_count=int(_number(action.get("click_count", 1), "click_count", 1, 2)))
        return result
    if kind == "key":
        key, code, event = action.get("key"), action.get("code", ""), action.get("event", "down")
        if not isinstance(key, str) or not key or len(key) > 32:
            raise ValueError("无效的键盘按键")
        if not isinstance(code, str) or len(code) > 32 or event not in {"down", "up"}:
            raise ValueError("无效的键盘事件")
        return {"type": kind, "event": event, "key": " " if key == "Space" else key,
                "code": code, "modifiers": int(_number(action.get("modifiers", 0), "modifiers", 0, 15)),
                "repeat": bool(action.get("repeat", False))}
    raise ValueError("不支持此实时浏览器操作")


class RemoteBrowserTransport:
    def __init__(self, debugger_address: str, target_id: str,
                 emit: Callable[[str, str], None] | None = None):
        parsed = urlsplit("http://" + debugger_address)
        if not parsed.hostname or not _loopback_host(parsed.hostname) or not parsed.port:
            raise ValueError("实时浏览器仅连接本机浏览器调试端口")
        if parsed.username or parsed.password or parsed.path not in ("", "/"):
            raise ValueError("无效的本机浏览器调试地址")
        self._host, self._port = parsed.hostname, parsed.port
        # Some managed Windows images resolve ``localhost`` to IPv6 while
        # Chromium's ephemeral DevTools port listens only on IPv4.  Keep both
        # loopback families available and remember the one that answered.
        self._connect_hosts = ("127.0.0.1", "::1") if self._host.lower() == "localhost" else (self._host,)
        self._connect_host = self._connect_hosts[0]
        self.target_id = str(target_id).removeprefix("CDwindow-")
        self._emit = emit or (lambda *_: None)
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._ws = None
        self._actions: deque[dict] = deque()
        self._sequence = 0
        self._data: bytes | None = None
        self._width = self._height = 0
        self._viewport_override: tuple[int, int] | None = None
        self._quality = "high"
        self._generation = 0
        self._viewport_init_id: int | None = None
        self._metrics_id: int | None = None
        self._metrics_ready = False
        self._screencast_active = False
        self._compositor_ready = False
        self._capture_id: int | None = None
        self._capture_generation = 0
        self._capture_started_at = 0.0
        self._input_sync_id: int | None = None
        self._next_capture_at = 0.0
        self._connected = False
        self._error: str | None = None
        self._command_id = 0
        self._pending: dict[int, str] = {}
        self._barriers: dict[int, threading.Event] = {}
        self._pointer = (0.0, 0.0)
        self._pressed_buttons = 0
        self._pressed_keys: dict[str, dict] = {}

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop = threading.Event()
            self._thread = threading.Thread(target=self._run, name="xhs-browser-stream", daemon=True)
            self._thread.start()

    def wait_until_ready(self, timeout: float = 12) -> bool:
        """Wait until the first usable frame arrives or the deadline expires."""
        deadline = time.monotonic() + max(0, timeout)
        while time.monotonic() < deadline and not self._stop.is_set():
            with self._lock:
                if self._connected and self._data:
                    return True
                thread = self._thread
            if thread is not None and not thread.is_alive():
                return False
            self._stop.wait(.05)
        with self._lock:
            return bool(self._connected and self._data)

    def attach(self, target_id: str) -> None:
        """Called by Selenium's owner after a new tab becomes active."""
        target_id = str(target_id).removeprefix("CDwindow-")
        with self._lock:
            unchanged = target_id == self.target_id and self._thread and self._thread.is_alive()
        if unchanged:
            return
        self.close()
        with self._lock:
            self.target_id = target_id
            self._data = None
            self._width = self._height = 0
            self._error = None
        self.start()

    def close(self) -> None:
        self._stop.set()
        with self._lock:
            connection, thread = self._ws, self._thread
            self._connected = False
            self._actions.clear()
        if thread and thread is not threading.current_thread():
            # recv has a short timeout, allowing orderly key/button release first.
            thread.join(timeout=3)
            if thread.is_alive() and connection:
                try:
                    connection.shutdown()
                except Exception:
                    pass
                thread.join(timeout=2)
        with self._lock:
            self._thread = None

    detach = close

    def snapshot(self) -> dict:
        with self._lock:
            return {"data": self._data, "sequence": self._sequence,
                    "width": self._width, "height": self._height,
                    "quality": self._quality, "pixel_ratio": 2 if self._quality == "high" else 1,
                    "connected": self._connected, "error": self._error}

    def dispatch(self, action: dict) -> None:
        value = normalize_action(action)
        with self._lock:
            if not self._connected:
                raise ValueError("浏览器实时连接尚未就绪，请稍后重试")
            # Never discard a down/up edge. Consecutive moves may share a frame.
            if (value["type"] == "pointer" and value["event"] == "move"
                    and self._actions and self._actions[-1].get("type") == "pointer"
                    and self._actions[-1].get("event") == "move"):
                self._actions[-1] = value
            else:
                self._actions.append(value)

    def barrier(self, timeout: float = 2) -> bool:
        """Release manual inputs and acknowledge them before automation resumes."""
        done = threading.Event()
        with self._lock:
            if not self._connected:
                return False
            self._actions.append({"type": "barrier", "done": done})
        return done.wait(timeout=max(0, timeout))

    def _endpoint(self) -> str:
        targets = None
        last_error = None
        for host in self._connect_hosts:
            connection = http.client.HTTPConnection(host, self._port, timeout=2)
            try:
                connection.request("GET", "/json/list")
                response = connection.getresponse()
                if response.status != 200:
                    raise RuntimeError("浏览器调试端口暂不可用")
                targets = json.loads(response.read(2_000_000))
                self._connect_host = host
                break
            except (OSError, TimeoutError, http.client.HTTPException) as exc:
                last_error = exc
            finally:
                connection.close()
        if targets is None:
            raise RuntimeError("无法直连本机浏览器调试端口，请检查安全软件的本机网络拦截") from last_error
        target = next((item for item in targets if item.get("id") == self.target_id and item.get("type") == "page"), None)
        if target is None:
            raise RuntimeError("当前浏览器标签页已关闭")
        endpoint = target.get("webSocketDebuggerUrl", "")
        parsed = urlsplit(endpoint)
        if parsed.scheme != "ws" or not parsed.hostname or not _loopback_host(parsed.hostname) or parsed.port != self._port:
            raise RuntimeError("浏览器返回了无效的本机调试地址")
        return endpoint

    def _connect_websocket(self, endpoint: str):
        """Open CDP on a pre-connected loopback socket, never through a proxy.

        ``websocket-client`` otherwise re-reads HTTP_PROXY/HTTPS_PROXY.  The
        endpoint returned by Chromium may spell the same address differently
        (``localhost`` versus ``127.0.0.1``), defeating a host-only no-proxy
        list on corporate Windows machines.
        """
        raw = socket.create_connection((self._connect_host, self._port), timeout=2)
        try:
            return websocket.create_connection(
                endpoint, timeout=2, suppress_origin=True, enable_multithread=True,
                http_no_proxy=["*"], socket=raw)
        except Exception:
            raw.close()
            raise

    def _send(self, method: str, params: dict | None = None, track: bool = False) -> int:
        self._command_id += 1
        identity = self._command_id
        if track:
            self._pending[identity] = method
        self._ws.send(json.dumps({"id": identity, "method": method, "params": params or {}}, ensure_ascii=False))
        return identity

    def _run(self) -> None:
        retry_delay = .25
        warning_reported = False
        while not self._stop.is_set():
            connection = None
            preserve_actions = False
            try:
                endpoint = self._endpoint()
                if self._stop.is_set():
                    break
                try:
                    connection = self._connect_websocket(endpoint)
                except (OSError, TimeoutError, websocket.WebSocketException) as exc:
                    raise RuntimeError("无法建立本机浏览器画面通道，请检查代理或安全软件的本机连接规则") from exc
                connection.settimeout(.025)
                with self._lock:
                    self._ws = connection
                self._pending.clear()
                self._barriers.clear()
                self._capture_id = None
                self._input_sync_id = None
                self._metrics_id = None
                self._metrics_ready = False
                self._screencast_active = False
                self._compositor_ready = False
                self._send("Page.enable", track=True)
                self._send("Page.bringToFront", track=True)
                if self._viewport_override:
                    self._configure_viewport(*self._viewport_override, self._quality)
                else:
                    # Read full CSS dimensions, including the scrollbar. Starting
                    # automation must be HD even before a viewer sends a resize.
                    self._viewport_init_id = self._send("Runtime.evaluate", {
                        "expression": "({width:innerWidth,height:innerHeight})",
                        "returnByValue": True}, track=True)
                retry_delay = .25
                while not self._stop.is_set():
                    if self._capture_id is not None and time.monotonic() - self._capture_started_at > 3:
                        raise RuntimeError("浏览器高清画面响应超时")
                    with self._lock:
                        # captureScreenshot temporarily changes Chromium's
                        # emulation scale until its response restores it. Every
                        # input (not only resize) must wait for that restoration.
                        count = (min(64, len(self._actions))
                                 if self._capture_id is None and self._metrics_ready else 0)
                        actions = [self._actions.popleft() for _ in range(count)]
                    for index, action in enumerate(actions):
                        self._execute(action)
                        if not self._metrics_ready:
                            # A resize and the following pointer may arrive in
                            # one WebSocket batch; wait for the metric ACK too.
                            with self._lock:
                                self._actions.extendleft(reversed(actions[index + 1:]))
                            break
                    if actions:
                        self._input_sync_id = self._send("Runtime.evaluate", {"expression": "void 0"}, track=True)
                        self._next_capture_at = time.monotonic() + .08
                    self._maybe_capture()
                    try:
                        raw = connection.recv()
                    except websocket.WebSocketTimeoutException:
                        continue
                    if not raw:
                        raise RuntimeError("浏览器实时连接已断开")
                    self._receive(json.loads(raw))
            except _CaptureInterrupted:
                # Navigation can abandon a capture callback without an error
                # response. Reattach the same target promptly; keep pending
                # manual actions so there is no dropped click/key transition.
                preserve_actions = True
                retry_delay = .025
            except Exception as exc:
                if not self._stop.is_set():
                    # The URL may contain signed page links; report only a safe diagnosis.
                    description = (str(exc) if isinstance(exc, RuntimeError)
                                   else f"实时画面连接暂不可用（{type(exc).__name__}）")
                    with self._lock:
                        self._error = description
                    if not warning_reported:
                        self._emit("warning", description + "；正在重新连接，自动化浏览器仍可继续使用")
                        warning_reported = True
            finally:
                if connection and self._ws is connection:
                    try:
                        self._release()
                        self._send("Page.stopScreencast")
                    except Exception:
                        pass
                with self._lock:
                    self._connected = False
                    if not preserve_actions:
                        self._actions.clear()
                    self._pressed_buttons = 0
                    self._pressed_keys.clear()
                    self._ws = None
                if connection:
                    try:
                        connection.close(timeout=.1)
                    except Exception:
                        pass
            if not self._stop.is_set():
                self._stop.wait(retry_delay)
                retry_delay = min(2, retry_delay * 2)

    def _receive(self, message: dict) -> None:
        if (message.get("method") == "Page.frameNavigated"
                and not message.get("params", {}).get("frame", {}).get("parentId")
                and self._capture_id is not None):
            raise _CaptureInterrupted()
        if message.get("method") == "Page.screencastFrame":
            params = message["params"]
            self._send("Page.screencastFrameAck", {"sessionId": params["sessionId"]})
            if self._metrics_ready:
                self._compositor_ready = True
            if self._quality != "smooth" or not self._metrics_ready:
                return
            data = base64.b64decode(params["data"], validate=True)
            metadata = params.get("metadata", {})
            with self._lock:
                self._data = data
                self._sequence += 1
                # CDP metadata is DIP, whereas JPEG dimensions can be physical
                # pixels. Explicit emulation dimensions are the CSS input space.
                width, height = self._viewport_override or (
                    metadata.get("deviceWidth", self._width), metadata.get("deviceHeight", self._height))
                self._width, self._height = max(1, round(width)), max(1, round(height))
                self._connected = True
                self._error = None
            return
        identity = message.get("id")
        if identity is None:
            return
        method = self._pending.pop(identity, None)
        if "error" in message and method:
            raise RuntimeError("浏览器未能启动实时画面，请尝试重新打开浏览器")
        if identity == self._viewport_init_id:
            self._viewport_init_id = None
            viewport = message.get("result", {}).get("result", {}).get("value", {})
            self._configure_viewport(
                int(_number(viewport.get("width"), "width", 320, 1920)),
                int(_number(viewport.get("height"), "height", 240, 1200)), self._quality)
        if identity == self._metrics_id:
            self._metrics_id = None
            self._metrics_ready = True
            high = self._quality == "high"
            # Keep the compositor producing surfaces even in a background
            # headless tab. High mode ACKs these frames but publishes only native
            # 2x captures; the first screencast frame also gates initial capture.
            self._send("Page.startScreencast", {"format": "jpeg", "quality": 92 if high else 80,
                       "maxWidth": 3840 if high else 1920, "maxHeight": 2400 if high else 1200,
                       "everyNthFrame": 1}, track=True)
            self._screencast_active = True
            self._next_capture_at = 0
        if identity == self._capture_id:
            self._capture_id = None
            if self._capture_generation == self._generation and self._quality == "high":
                data = base64.b64decode(message["result"]["data"], validate=True)
                with self._lock:
                    self._data = data
                    self._sequence += 1
                    self._connected = True
                    self._error = None
        if identity == self._input_sync_id:
            self._input_sync_id = None
        barrier = self._barriers.pop(identity, None)
        if barrier:
            barrier.set()

    def _configure_viewport(self, width: int, height: int, quality: str) -> None:
        if (self._metrics_ready and self._viewport_override == (width, height)
                and self._quality == quality):
            return
        if self._screencast_active:
            self._send("Page.stopScreencast", track=True)
            self._screencast_active = False
        with self._lock:
            self._viewport_override = (width, height)
            self._width, self._height = width, height
            self._quality = quality
            self._generation += 1
        self._metrics_ready = False
        self._compositor_ready = False
        self._metrics_id = self._send("Emulation.setDeviceMetricsOverride", {
            "width": width, "height": height,
            "deviceScaleFactor": 2 if quality == "high" else 1, "mobile": False}, track=True)

    def _maybe_capture(self) -> None:
        if (self._quality != "high" or not self._metrics_ready
                or self._capture_id is not None or self._input_sync_id is not None
                or time.monotonic() < self._next_capture_at):
            return
        with self._lock:
            if self._actions:
                return  # Inputs and viewport changes take priority over a frame.
        # startScreencast can output only CSS-resolution frames on Windows and
        # some Edge builds do not emit its first event for an initial blank page.
        # Capture directly as soon as metrics are ready; the command safely
        # retries with the transport if the compositor surface is still cold.
        # 1920x1200 CSS becomes at most 3840x2400 real image pixels.
        # Never scale an existing screenshot, and keep only one capture in flight.
        self._capture_generation = self._generation
        self._capture_started_at = time.monotonic()
        self._capture_id = self._send("Page.captureScreenshot", {
            "format": "jpeg", "quality": 92, "fromSurface": True,
            "captureBeyondViewport": False}, track=True)
        self._next_capture_at = time.monotonic() + .125

    def _execute(self, action: dict) -> None:
        kind = action["type"]
        if kind == "barrier":
            self._release()
            identity = self._send("Runtime.evaluate", {"expression": "void 0"}, track=True)
            self._barriers[identity] = action["done"]
            return
        if kind == "resize":
            self._configure_viewport(action["width"], action["height"], action.get("quality", "high"))
            return
        if kind == "text":
            if action["text"]:
                self._send("Input.insertText", {"text": action["text"]})
            return
        if kind == "release":
            self._release()
            return
        if kind == "key":
            params = self._key_params(action)
            self._send("Input.dispatchKeyEvent", params)
            identity = action["code"] or action["key"]
            if action["event"] == "down":
                self._pressed_keys[identity] = action
            else:
                self._pressed_keys.pop(identity, None)
            return
        with self._lock:
            width, height = max(1, self._width - 1), max(1, self._height - 1)
        x, y = action["x"] * width, action["y"] * height
        self._pointer = (x, y)
        params = {"x": x, "y": y, "modifiers": action["modifiers"]}
        if kind == "wheel":
            params.update(type="mouseWheel", deltaX=action["delta_x"], deltaY=action["delta_y"])
        else:
            event, button = action["event"], action["button"]
            if event == "down":
                self._pressed_buttons |= _BUTTONS[button]
            elif event == "up":
                self._pressed_buttons &= ~_BUTTONS[button]
            params.update(type={"down": "mousePressed", "move": "mouseMoved", "up": "mouseReleased"}[event],
                          button=button if event != "move" or self._pressed_buttons else "none",
                          buttons=self._pressed_buttons,
                          clickCount=action["click_count"] if event != "move" else 0)
        self._send("Input.dispatchMouseEvent", params)

    @staticmethod
    def _key_params(action: dict) -> dict:
        key, code, modifiers = action["key"], action["code"], action["modifiers"]
        virtual_key = _KEY_CODES.get(key, 0)
        if len(code) == 4 and code.startswith("Key") and code[-1].isascii():
            virtual_key = ord(code[-1].upper())
        elif len(code) == 6 and code.startswith("Digit") and code[-1].isdigit():
            virtual_key = ord(code[-1])
        elif len(key) == 1 and key.isascii():
            virtual_key = ord(key.upper())
        elif key.startswith("F") and key[1:].isdigit() and 1 <= int(key[1:]) <= 12:
            virtual_key = 111 + int(key[1:])
        params = {"type": "keyUp" if action["event"] == "up" else "keyDown",
                  "key": key, "code": code, "modifiers": modifiers,
                  "windowsVirtualKeyCode": virtual_key, "autoRepeat": action["repeat"]}
        if action["event"] == "down":
            if not modifiers & (2 | 4) and len(key) == 1:
                params["text"] = key
                params["unmodifiedText"] = key.lower()
            elif key == "Enter":
                params["text"] = "\r"
            if modifiers & (2 | 4) and not modifiers & 1:
                command = {"a": "selectAll", "c": "copy", "x": "cut", "y": "redo",
                           "z": "redo" if modifiers & 8 else "undo"}.get(key.lower())
                if command:
                    params["commands"] = [command]
        return params

    def _release(self) -> None:
        for action in reversed(list(self._pressed_keys.values())):
            self._send("Input.dispatchKeyEvent", self._key_params({**action, "event": "up", "modifiers": 0}))
        self._pressed_keys.clear()
        for button, mask in _BUTTONS.items():
            if self._pressed_buttons & mask:
                self._pressed_buttons &= ~mask
                self._send("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": self._pointer[0],
                           "y": self._pointer[1], "button": button, "buttons": self._pressed_buttons,
                           "clickCount": 1, "modifiers": 0})
