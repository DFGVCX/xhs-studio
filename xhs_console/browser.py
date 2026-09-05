"""A single, persistent browser session for the local Xiaohongshu console.

Every Selenium call belongs to the worker thread. ``checkpoint`` lets that worker
process preview/input requests and pause/stop while the page is settling.
"""

from __future__ import annotations

import base64
import io
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit


NOTE_PATH = re.compile(r"/(?:explore|discovery/item|search_result)/([a-fA-F0-9]{24})(?:/|$)")
ALLOWED_HOSTS = ("xiaohongshu.com", "xhslink.com")
XHS_HOME = "https://www.xiaohongshu.com/explore"


class NeedsInteraction(RuntimeError):
    """A login or verification page requires a person to continue."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class NoteUnavailable(RuntimeError):
    """The requested note is unavailable or its detail view did not load."""


def note_id_from_url(url: str) -> str | None:
    match = NOTE_PATH.search(urlsplit(url).path)
    return match.group(1).lower() if match else None


def validate_navigation_url(url: str) -> str:
    """Accept complete web URLs while preserving scheme, port and fragment.

    Manual browsing is independent of Xiaohongshu collection. Reject ambiguous
    authority syntax before Chromium can reinterpret it, without a host allowlist.
    """
    if not isinstance(url, str) or re.search(r"[\x00-\x1f\x7f-\x9f\\]", url):
        raise ValueError("网页链接不能包含控制字符或反斜杠")
    value = url.strip()
    if any(character.isspace() for character in value):
        raise ValueError("网页链接中的空格需要使用 URL 编码")
    try:
        parsed = urlsplit(value)
        host, port = parsed.hostname, parsed.port
    except ValueError as exc:
        raise ValueError("网页链接的主机或端口无效") from exc
    if parsed.scheme not in ("https", "http") or not host:
        raise ValueError("请输入完整的 http:// 或 https:// 网页链接")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("网页链接不能包含账号或密码")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("网页链接的端口必须介于 1 和 65535")
    authority = parsed.netloc
    if authority.startswith("["):
        suffix = authority[authority.index("]") + 1:]
        if suffix and not re.fullmatch(r":[0-9]+", suffix):
            raise ValueError("网页链接的 IPv6 主机或端口无效")
    elif ":" in authority and not re.fullmatch(r"[^:]+:[0-9]+", authority):
        raise ValueError("网页链接的端口无效")
    if re.search(r'[<>"{}|^`]', host):
        raise ValueError("网页链接的主机无效")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", parsed.query, parsed.fragment))


def normalize_xhs_url(url: str) -> str:
    """Keep the collection allowlist and historical HTTPS canonicalization."""
    parsed = urlsplit(validate_navigation_url(url))
    host = (parsed.hostname or "").lower()
    if not any(host == allowed or host.endswith("." + allowed) for allowed in ALLOWED_HOSTS):
        raise ValueError("笔记采集仅支持小红书及 xhslink.com 链接")
    if parsed.port not in (None, 80, 443):
        raise ValueError("笔记采集链接不能包含自定义端口")
    return urlunsplit(("https", host, parsed.path or "/", parsed.query, ""))


def normalize_note_url(url: str, base: str = "https://www.xiaohongshu.com") -> str | None:
    """Keep the original signed query intact; never synthesize an access token."""
    try:
        value = normalize_xhs_url(urljoin(base, str(url).replace("&amp;", "&")))
    except (TypeError, ValueError):
        return None
    return value if note_id_from_url(value) else None


def deduplicate_note_urls(urls: list[str]) -> list[str]:
    """Maintain discovery order, preferring signed hrefs for duplicate note IDs."""
    found: dict[str, str] = {}
    for raw in urls:
        url = normalize_note_url(raw)
        if not url:
            continue
        note_id = note_id_from_url(url)
        old = found.get(note_id)
        signed = bool(parse_qs(urlsplit(url).query).get("xsec_token"))
        old_signed = old and bool(parse_qs(urlsplit(old).query).get("xsec_token"))
        if old is None or (signed and not old_signed):
            found[note_id] = url
    return list(found.values())


def normalize_note(raw: dict, url: str) -> dict:
    """Normalize extraction without fabricating missing dates or authors."""
    result = dict(raw)
    result["note_id"] = str(raw.get("note_id") or note_id_from_url(url) or "")
    result["url"] = url
    for key in ("title", "author", "content", "location", "source"):
        result[key] = str(raw.get(key) or "").strip()
    published = raw.get("published_at")
    if isinstance(published, (int, float)) and not isinstance(published, bool):
        try:
            seconds = published / 1000 if published > 100_000_000_000 else published
            published = datetime.fromtimestamp(seconds, timezone(timedelta(hours=8))).isoformat(timespec="seconds")
        except (OverflowError, OSError, ValueError):
            published = str(published)
    result["published_at"] = str(published or "").strip()
    images = []
    for item in raw.get("images") or []:
        if not isinstance(item, str):
            continue
        value = "https:" + item if item.startswith("//") else item
        parsed = urlsplit(value)
        if parsed.scheme in ("http", "https") and parsed.hostname and value not in images:
            images.append(value)
    result["images"] = images
    result["type"] = "video" if raw.get("type") in ("video", "视频") else ("image" if images else "text")
    return result


def bundled_chrome_paths(resource_root: Path | None = None) -> tuple[str, str] | None:
    """Return the release's matched Chrome/ChromeDriver pair when available."""
    if resource_root is None:
        root = Path(sys._MEIPASS).resolve() if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[1]
    else:
        root = Path(resource_root).resolve()
    browser = root / "browser" / "chrome-win64" / "chrome.exe"
    driver = root / "browser" / "chromedriver-win64" / "chromedriver.exe"
    if browser.is_file() and driver.is_file():
        return str(browser), str(driver)
    return None


def browser_candidates(preference: str = "auto", resource_root: Path | None = None) -> list[tuple[str, str | None]]:
    """Discover bundled/system browsers and retain managed-browser fallbacks."""
    if preference not in ("auto", "chrome", "edge"):
        raise ValueError("浏览器必须为 auto、chrome 或 edge")
    roots = [os.environ.get("PROGRAMFILES"), os.environ.get("PROGRAMFILES(X86)"), os.environ.get("LOCALAPPDATA")]
    names = ("chrome", "edge") if preference == "auto" else (preference,)
    candidates = []
    bundled = bundled_chrome_paths(resource_root)
    if bundled and preference in ("auto", "chrome"):
        candidates.append(("chrome", bundled[0]))
    for name in names:
        executable = "chrome.exe" if name == "chrome" else "msedge.exe"
        relative = "Google/Chrome/Application/chrome.exe" if name == "chrome" else "Microsoft/Edge/Application/msedge.exe"
        known = [str(Path(root) / relative) for root in roots if root]
        on_path = shutil.which(executable) or shutil.which("google-chrome" if name == "chrome" else "microsoft-edge")
        if on_path:
            known.insert(0, on_path)
        found = next((path for path in known if Path(path).is_file()), None)
        if found and not any(candidate_name == name and Path(candidate_path or "") == Path(found) for candidate_name, candidate_path in candidates):
            candidates.append((name, found))
    if bundled and preference == "edge":
        # Treat the explicit engine as a preference, not a reason to make the
        # portable release unusable when that machine's Edge driver is blocked.
        candidates.append(("chrome", bundled[0]))
    # A discovered browser can still fail when its matching driver is unavailable.
    # Keep an independently managed fallback for every missing engine instead of
    # stopping after the first installed browser (the former Edge-only failure).
    for name in names:
        if not any(candidate_name == name for candidate_name, _ in candidates):
            candidates.append((name, None))
    return candidates


def browser_connection_arguments(direct_connection: bool) -> tuple[str, ...]:
    """Return explicit Chromium proxy arguments for the selected network mode.

    ``--no-proxy-server`` ignores system/PAC HTTP proxies. It intentionally does
    not claim to bypass VPN/TUN or router-level routing, which sits below the
    browser's proxy configuration.
    """
    return ("--no-proxy-server", "--proxy-bypass-list=*") if direct_connection else ()


def browser_display_arguments(embedded_only: bool) -> tuple[str, ...]:
    """Keep an ordinary Chromium renderer off-screen for the embedded viewer.

    Xiaohongshu currently restricts Chromium's native headless mode even when
    the same clean profile and network work in a normal window. The embedded
    viewer therefore streams a regular, off-screen window instead of enabling
    ``--headless``. Background-throttling flags keep CDP frames responsive.
    """
    if not embedded_only:
        return ()
    return (
        "--window-position=-32000,-32000",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--disable-features=CalculateNativeWinOcclusion",
    )


def configure_browser_binary(options, binary: str | None) -> None:
    """Use an installed browser or let Selenium manage stable Chrome/Edge."""
    if binary:
        options.binary_location = binary
    else:
        options.browser_version = "stable"


def stop_service_safely(service) -> None:
    """Never let cleanup hide the browser's original startup exception."""
    if service is None:
        return
    try:
        service.stop()
    except Exception:
        # Selenium's Service may not create ``process`` when driver discovery,
        # download, permissions, or process creation fails before start().
        pass


ACCESS_SCRIPT = r"""
const visible = el => !!el && el.getBoundingClientRect().width > 0 && el.getBoundingClientRect().height > 0 && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none';
const firstVisible = selector => Array.from(document.querySelectorAll(selector)).find(visible);
const restrictedRoute = /\/(?:website-login|web-login)\/error(?:\/|$)/.test(location.pathname);
const errorView = firstVisible('.error-wrapper, .error-page, [role="dialog"]');
const hasContent = document.querySelector('#noteContainer, .note-detail, .note-item');
const restrictionText = (errorView?.innerText || ((restrictedRoute || !hasContent) ? document.body?.innerText : '') || '').slice(0, 4000);
const restrictionReasons = [...new Set(restrictionText.match(/IP\s*存在风险|网络环境存在风险|安全限制|300012|访问受限/gi) || [])];
if (restrictedRoute || restrictionReasons.length) {
  const detail = restrictionReasons.slice(0, 3).join(' / ') || '请查看页面错误提示';
  return `小红书提示当前网络访问受限（${detail}），请检查原浏览器提示或稍后再试；完成处理后继续`;
}
if (firstVisible('#captcha-container, .captcha-container, .reds-captcha, [id*="captcha"], iframe[src*="captcha"], iframe[src*="verify"]')) return '页面出现安全验证，请在浏览器画面或原浏览器中完成验证，然后继续';
if (/\/(?:captcha|website-login|web-login)(?:\/|$)/.test(location.pathname)) return '当前页面需要登录或安全验证，请完成后继续';
const login = firstVisible('.login-container, .login-modal, .login-panel, .login-box, [class*="login-modal"], [class*="login-container"]');
if (login && /登录|扫码|手机号|验证码/.test(login.innerText || '')) return '请在当前浏览器中扫码或手动登录，完成后点击继续';
const dialogs = Array.from(document.querySelectorAll('[role="dialog"], .modal, .verify-container, .error-wrapper, .error-page')).filter(visible);
for (const dialog of dialogs) {
  const text = (dialog.innerText || '').slice(0, 1600);
  if (/安全验证|滑动验证|拖动滑块|操作频繁|访问频繁|网络环境存在风险|异常访问|访问受限/.test(text)) return '小红书要求验证或限制了当前访问，请在浏览器中处理后继续';
  if (/登录后|扫码登录|手机号登录/.test(text)) return '请先完成小红书登录，然后继续';
}
if (!document.querySelector('#noteContainer, .note-detail, .note-item')) {
  const text = (document.body?.innerText || '').slice(0, 3000);
  if (/安全验证|滑动验证|拖动滑块|操作频繁|访问频繁|网络环境存在风险|异常访问|访问受限/.test(text)) return '小红书要求验证或限制了当前访问，请在浏览器中处理后继续';
  if (/登录后查看搜索结果|登录后查看更多|请先登录|扫码登录/.test(text)) return '请先完成小红书登录，然后继续';
}
return null;
"""


class BrowserSession:
    def __init__(self, project_dir: Path, emit: Callable[[str, str], None], checkpoint: Callable[..., None]):
        self.project_dir = Path(project_dir).resolve()
        self.emit = emit
        self.checkpoint = checkpoint
        self.driver = None
        self.remote = None
        self._known_handles: set[str] = set()
        self.browser = ""
        self.direct_connection = True
        self._extract_script = Path(__file__).with_name("extract_note.js").read_text(encoding="utf-8")

    def open(self, headless: bool = False, browser: str = "auto", direct_connection: bool = True) -> None:
        if self.driver is not None:
            return
        from selenium import webdriver
        from selenium.webdriver.chrome.service import Service as ChromeService
        from selenium.webdriver.edge.service import Service as EdgeService

        manager_cache = self.project_dir / "runtime" / "selenium"
        manager_cache.mkdir(parents=True, exist_ok=True)
        os.environ.setdefault("SE_CACHE_PATH", str(manager_cache))
        os.environ.setdefault("SE_AVOID_STATS", "true")

        errors = []
        bundled = bundled_chrome_paths()
        for name, binary in browser_candidates(browser):
            profile = self.project_dir / "runtime" / "profiles" / name
            profile.mkdir(parents=True, exist_ok=True)
            options = webdriver.ChromeOptions() if name == "chrome" else webdriver.EdgeOptions()
            options.page_load_strategy = "eager"
            configure_browser_binary(options, binary)
            options.add_argument(f"--user-data-dir={profile}")
            options.add_argument("--window-size=1365,900")
            options.add_argument("--no-first-run")
            options.add_argument("--no-default-browser-check")
            for argument in browser_connection_arguments(direct_connection):
                options.add_argument(argument)
            for argument in browser_display_arguments(headless):
                options.add_argument(argument)
            service_type = ChromeService if name == "chrome" else EdgeService
            service_options = {"log_output": subprocess.DEVNULL}
            bundled_driver = None
            if name == "chrome" and bundled and binary and Path(binary).resolve() == Path(bundled[0]).resolve():
                bundled_driver = bundled[1]
                service_options["executable_path"] = bundled_driver
            if os.name == "nt":
                service_options["popen_kw"] = {"creation_flags": subprocess.CREATE_NO_WINDOW}
            service = None
            if bundled_driver:
                self.emit("info", "正在启动 Release 内置 Chrome，无需联网下载浏览器或驱动")
            else:
                managed = "浏览器和驱动" if binary is None else "驱动"
                self.emit("info", f"正在启动 {name.title()}，首次运行可能需要自动准备{managed}")
            try:
                service = service_type(**service_options)
                constructor = webdriver.Chrome if name == "chrome" else webdriver.Edge
                self.driver = constructor(options=options, service=service)
                self.browser = name
                self.direct_connection = direct_connection
                self.driver.set_page_load_timeout(30)
                self.driver.set_script_timeout(15)
                # A manually opened viewer is a general-purpose browser. Keep
                # its initial page neutral; collection methods navigate to XHS
                # explicitly when an automation job takes ownership.
                self.driver.get("about:blank")
                self._known_handles = set(self.driver.window_handles)
                try:
                    from .remote_browser import RemoteBrowserTransport
                    capabilities = self.driver.capabilities
                    debugging = capabilities.get("goog:chromeOptions") or capabilities.get("ms:edgeOptions") or {}
                    self.remote = RemoteBrowserTransport(debugging["debuggerAddress"], self.driver.current_window_handle, self.emit)
                    self.remote.start()
                except Exception as exc:
                    self.emit("warning", f"实时交互画面暂不可用（{type(exc).__name__}），仍可查看浏览器截图")
                network = "直连网络（已忽略系统 HTTP/HTTPS 代理）" if direct_connection else "跟随系统代理"
                self.emit("success", f"{name.title()} 已启动，当前使用{network}；登录状态保存在独立配置目录")
                return
            except Exception as exc:
                stop_service_safely(service)
                self.driver = None
                detail = str(exc).splitlines()[0].strip() or type(exc).__name__
                errors.append(f"{name}: {detail}")
        raise RuntimeError(
            "无法启动浏览器。免安装 Release 会优先使用内置的匹配版 Chrome；源码运行会尝试本机浏览器和联网备用浏览器。"
            "请检查网络、写入权限，并确认没有其他工作台占用同一浏览器配置。详情：" + "；".join(errors)
        )

    def close(self) -> None:
        remote, self.remote = self.remote, None
        if remote is not None:
            remote.close()
        driver, self.driver = self.driver, None
        self._known_handles.clear()
        if driver is not None:
            try:
                driver.quit()
            except Exception as exc:
                self.emit("warning", f"浏览器关闭时报告异常：{str(exc).splitlines()[0]}")

    def _require_driver(self):
        if self.driver is None:
            raise RuntimeError("浏览器尚未启动")
        return self.driver

    def navigate(self, url: str) -> None:
        from selenium.common.exceptions import TimeoutException
        driver = self._require_driver()
        validated = validate_navigation_url(url)
        try:
            driver.get(validated)
        except TimeoutException:
            # Eager navigation may time out while usable DOM has already arrived.
            self.emit("warning", "页面加载超时，正在检查已加载的内容")
        self.checkpoint(0.3)

    def screenshot(self) -> bytes:
        driver = self._require_driver()
        self._sync_active_tab()
        try:
            result = driver.execute_cdp_cmd("Page.captureScreenshot", {"format": "jpeg", "quality": 75, "captureBeyondViewport": False})
            return base64.b64decode(result["data"])
        except Exception:
            from PIL import Image
            with Image.open(io.BytesIO(driver.get_screenshot_as_png())) as image:
                output = io.BytesIO()
                image.convert("RGB").save(output, format="JPEG", quality=75)
                return output.getvalue()

    def info(self) -> dict:
        driver = self._require_driver()
        self._sync_active_tab()
        viewport = driver.execute_script("return {width: innerWidth, height: innerHeight};")
        return {"url": driver.current_url, "title": driver.title, "width": viewport["width"], "height": viewport["height"],
                "browser": self.browser, "network_mode": "direct" if self.direct_connection else "system"}

    def _sync_active_tab(self) -> None:
        """Follow tabs opened in the embedded viewport, on Selenium's owner only."""
        driver = self._require_driver()
        handles = driver.window_handles
        opened = [handle for handle in handles if handle not in self._known_handles]
        try:
            current = driver.current_window_handle
        except Exception:
            current = None
        if opened:
            driver.switch_to.window(opened[-1])
        elif current not in handles and handles:
            driver.switch_to.window(handles[-1])
        self._known_handles = set(handles)
        if self.remote is not None and handles:
            self.remote.attach(driver.current_window_handle)

    def interact(self, action: dict) -> None:
        from selenium.webdriver.common.keys import Keys
        driver = self._require_driver()
        kind = action.get("type")
        if kind in ("click", "scroll"):
            viewport = driver.execute_script("return {width: innerWidth, height: innerHeight};")
            x = max(0.0, min(1.0, float(action.get("x", 0.5)))) * max(1, viewport["width"] - 1)
            y = max(0.0, min(1.0, float(action.get("y", 0.5)))) * max(1, viewport["height"] - 1)
            if kind == "click":
                for event in ("mousePressed", "mouseReleased"):
                    driver.execute_cdp_cmd("Input.dispatchMouseEvent", {"type": event, "x": x, "y": y, "button": "left", "clickCount": 1})
            else:
                delta = max(-3000.0, min(3000.0, float(action.get("delta", 500))))
                driver.execute_cdp_cmd("Input.dispatchMouseEvent", {"type": "mouseWheel", "x": x, "y": y, "deltaX": 0, "deltaY": delta})
        elif kind == "text":
            driver.execute_cdp_cmd("Input.insertText", {"text": str(action.get("text", ""))[:4000]})
        elif kind == "key":
            keys = {"Enter": Keys.ENTER, "Tab": Keys.TAB, "Escape": Keys.ESCAPE, "Backspace": Keys.BACKSPACE,
                    "ArrowUp": Keys.ARROW_UP, "ArrowDown": Keys.ARROW_DOWN, "ArrowLeft": Keys.ARROW_LEFT,
                    "ArrowRight": Keys.ARROW_RIGHT, "Delete": Keys.DELETE, "Home": Keys.HOME, "End": Keys.END,
                    "PageDown": Keys.PAGE_DOWN, "PageUp": Keys.PAGE_UP, "Space": Keys.SPACE}
            key = action.get("key")
            if key in ("Ctrl+A", "Control+a", "Control+A"):
                driver.switch_to.active_element.send_keys(Keys.CONTROL, "a", Keys.NULL)
            elif key in keys:
                driver.switch_to.active_element.send_keys(keys[key])
            else:
                raise ValueError("不支持此按键")
        elif kind == "back":
            driver.back()
        elif kind == "forward":
            driver.forward()
        elif kind == "home":
            self.navigate(XHS_HOME)
        elif kind == "navigate":
            self.navigate(str(action.get("url", "")))
        elif kind == "refresh":
            driver.refresh()
        else:
            raise ValueError("不支持此浏览器操作")

    def access_status(self) -> str | None:
        return self._require_driver().execute_script(ACCESS_SCRIPT)

    def _check_access(self) -> None:
        reason = self.access_status()
        if reason:
            raise NeedsInteraction(reason)

    def _wait_overrun(self, seconds: float) -> float:
        """Do not consume page/search budgets while the owner handles a pause.

        The callback owns waiting and can stay inside a manual pause indefinitely.
        Its excess over the requested settling delay is control/preview overhead,
        not active collection time. Cancellation still propagates immediately.
        """
        started = time.monotonic()
        self.checkpoint(seconds)
        return max(0.0, time.monotonic() - started - seconds)

    def prepare_collection(self, config) -> None:
        """Open the XHS home page and require a real persisted login session.

        Browser startup and a rendered page are not evidence of authentication.
        A collection run proceeds only after Xiaohongshu exposes its login cookie;
        access restrictions are reported before the login check so an IP-risk page
        can never be presented as a login prompt or a successful login.
        """
        driver = self._require_driver()
        driver.set_page_load_timeout(config.page_timeout)
        self.emit("info", "开始采集：正在进入小红书首页并检查登录状态")
        self.navigate(XHS_HOME)
        self.checkpoint(1)
        self._check_access()
        session = driver.get_cookie("web_session")
        if not session or not str(session.get("value") or "").strip():
            raise NeedsInteraction("已进入小红书首页，但尚未检测到登录会话；请在上方完成登录，完成后点击「已处理，继续」")
        self.emit("success", "已进入小红书首页，并检测到本机保存的登录会话")

    def search(self, config) -> list[str]:
        driver = self._require_driver()
        driver.set_page_load_timeout(config.page_timeout)
        target = "https://www.xiaohongshu.com/search_result?" + urlencode({"keyword": config.keyword, "source": "web_explore_feed"})
        self.navigate(target)
        self.checkpoint(1)
        started = time.monotonic()
        paused_time = 0.0
        found: list[str] = []
        unchanged = 0
        while time.monotonic() - started - paused_time < config.search_seconds:
            self._check_access()
            hrefs = driver.execute_script("return Array.from(document.querySelectorAll('a[href*=\"/explore/\"],a[href*=\"/discovery/item/\"],a[href*=\"/search_result/\"]')).map(a=>a.href);")
            previous = len(found)
            found = deduplicate_note_urls(found + hrefs)
            if len(found) > previous:
                unchanged = 0
                self.emit("info", f"搜索已发现 {min(len(found), config.max_notes)} 篇笔记")
            else:
                unchanged += 1
            if len(found) >= config.max_notes:
                break
            if unchanged >= 7:
                self.emit("info", "连续多次滚动没有发现新笔记，结束搜索")
                break
            driver.execute_script("window.scrollBy({top: Math.round(innerHeight * 0.8), behavior: 'smooth'});")
            remaining = config.search_seconds - (time.monotonic() - started - paused_time)
            if remaining > 0:
                paused_time += self._wait_overrun(min(max(float(config.interval_seconds), 1.5), remaining))
        self._check_access()
        if not found:
            raise NoteUnavailable("搜索结果中没有可读取的笔记链接；请检查登录状态、关键词或当前页面")
        return found[: config.max_notes]

    def extract(self, url: str, config) -> dict:
        driver = self._require_driver()
        driver.set_page_load_timeout(config.page_timeout)
        self.navigate(url)
        deadline = time.monotonic() + config.page_timeout
        while time.monotonic() < deadline:
            self._check_access()
            current_url = driver.current_url
            expected_id = note_id_from_url(current_url) or note_id_from_url(url)
            raw = driver.execute_script(self._extract_script, expected_id)
            if raw and raw.get("unavailable"):
                raise NoteUnavailable(raw["unavailable"])
            if raw and raw.get("ready"):
                raw.pop("ready", None)
                return normalize_note(raw, current_url)
            deadline += self._wait_overrun(0.5)
        raise NoteUnavailable("未找到完整笔记详情，可能是链接失效、页面结构变化或访问受限；可在实时画面中检查")
