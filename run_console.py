"""Start the local console; all paths are anchored to this file."""

import argparse

import uvicorn


def main():
    parser = argparse.ArgumentParser(description="小红书采集工作台")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("端口必须介于 1024 和 65535")
    print(f"工作台地址：http://127.0.0.1:{args.port}", flush=True)
    uvicorn.run("xhs_console.server:app", host="127.0.0.1", port=args.port, log_level="info")


if __name__ == "__main__":
    main()
