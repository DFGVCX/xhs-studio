"""Start the local console from source or a packaged Windows release."""

import argparse
import json
import threading
import time
import webbrowser
from urllib.error import URLError
from urllib.request import urlopen

import uvicorn

from xhs_console.server import app


def server_is_ready(url: str) -> bool:
    try:
        with urlopen(f"{url}/api/state", timeout=0.5) as response:
            return response.status == 200 and isinstance(json.load(response), dict)
    except (OSError, URLError, ValueError, json.JSONDecodeError):
        return False


def open_when_ready(url: str) -> None:
    for _ in range(120):
        if server_is_ready(url):
            webbrowser.open(url)
            return
        time.sleep(0.25)


def main():
    parser = argparse.ArgumentParser(description="小红书采集工作台")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-open", action="store_true", help="启动后不自动打开系统浏览器")
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("端口必须介于 1024 和 65535")
    url = f"http://127.0.0.1:{args.port}"
    print("红薯工作台正在启动，请保留此窗口……", flush=True)
    print(f"工作台地址：{url}", flush=True)
    if server_is_ready(url):
        print("工作台已经在运行，正在打开页面。", flush=True)
        if not args.no_open:
            webbrowser.open(url)
        return
    if not args.no_open:
        threading.Thread(target=open_when_ready, args=(url,), daemon=True).start()
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="info")


if __name__ == "__main__":
    main()
