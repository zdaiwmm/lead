"""Loopback-only playground. Simulated orders forward exclusively into pixel test scope."""

import argparse
import json
import re
import sqlite3
import threading
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
ORDER_LOCK = threading.Lock()


def run(port=8787, config_path=None):
    config_path = Path(config_path or ROOT / ".state/pixel-playground.json")
    origin = f"http://127.0.0.1:{port}"
    orders_path = ROOT / ".state/pixel-playground-orders.sqlite3"
    with sqlite3.connect(orders_path) as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS orders(id TEXT PRIMARY KEY, quantity INTEGER, user_id TEXT, attribution TEXT, paid_at TEXT)"
        )
    orders_path.chmod(0o600)

    def config():
        value = json.loads(config_path.read_text())
        collect = urlsplit(value["collect_origin"])
        if (
            collect.scheme != "http"
            or collect.hostname != "127.0.0.1"
            or collect.username
            or collect.password
            or collect.path not in ("", "/")
        ):
            raise ValueError("测试后端只允许向本机采集服务回传")
        return value

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(Path(__file__).parent), **kwargs)

        def log_message(self, *args):
            pass

        def reply(self, status, body):
            data = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == "/test-api/status":
                try:
                    value = config()
                    self.reply(200, {"enabled": True, "snippet": value["snippet"]})
                except (OSError, ValueError, KeyError):
                    self.reply(200, {"enabled": False})
            else:
                super().do_GET()

        def do_POST(self):
            if (
                self.headers.get("Origin") != origin
                or self.headers.get_content_type() != "application/json"
            ):
                self.reply(403, {"detail": "仅允许测试页面的同源请求"})
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 16000:
                    raise ValueError("请求大小无效")
                body = json.loads(self.rfile.read(size))
                value = config()
                if self.path == "/test-api/orders":
                    source = body.get("lead_attribution")
                    quantity = body.get("quantity")
                    user = body.get("user_id")
                    if (
                        not isinstance(source, dict)
                        or source.get("connection_id") != value["connection_id"]
                        or not re.fullmatch(
                            r"lt_[A-Za-z0-9_-]{32}", str(source.get("test_code", ""))
                        )
                    ):
                        raise ValueError(
                            "请先从后台打开完整流程测试链接；模拟订单只允许测试上下文"
                        )
                    if type(quantity) is not int or not 1 <= quantity <= 100:
                        raise ValueError("测试商品数量须为 1 到 100")
                    if user is not None and (
                        not isinstance(user, str) or not 1 <= len(user) <= 128
                    ):
                        raise ValueError("测试用户编号无效")
                    oid = "demo-order-" + str(uuid4())
                    with sqlite3.connect(orders_path) as db:
                        db.execute(
                            "INSERT INTO orders VALUES(?,?,?,?,NULL)",
                            (oid, quantity, user, json.dumps(source)),
                        )
                    self.reply(
                        200, {"order_id": oid, "value": quantity * 99, "scope": "test"}
                    )
                    return
                match = re.fullmatch(
                    r"/test-api/orders/(demo-order-[0-9a-f-]{36})/pay", self.path
                )
                if not match:
                    self.reply(404, {"detail": "接口不存在"})
                    return
                oid = match[1]
                with ORDER_LOCK, sqlite3.connect(orders_path) as db:
                    order = db.execute(
                        "SELECT quantity,user_id,attribution,paid_at FROM orders WHERE id=?",
                        (oid,),
                    ).fetchone()
                    if not order:
                        self.reply(404, {"detail": "测试订单不存在"})
                        return
                    quantity, user, saved, paid_at = order
                    source = json.loads(saved)
                    if (
                        not source.get("test_code")
                        or source.get("connection_id") != value["connection_id"]
                    ):
                        raise ValueError("测试订单连接不匹配，不能回传")
                    paid_at = paid_at or datetime.now(timezone.utc).isoformat()
                    db.execute("UPDATE orders SET paid_at=? WHERE id=?", (paid_at, oid))
                payload = {
                    "connection_id": value["connection_id"],
                    "event_name": "Purchase",
                    "event_id": "purchase:" + oid,
                    "occurred_at": paid_at,
                    "user_id": user,
                    "lead_attribution": source,
                    "properties": {
                        "order_id": oid,
                        "value": quantity * 99,
                        "currency": "CNY",
                    },
                }
                request = Request(
                    value["collect_origin"].rstrip("/") + "/collect/server/events",
                    data=json.dumps(payload).encode(),
                    method="POST",
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": "Bearer " + value["server_key"],
                    },
                )
                with urlopen(request, timeout=15) as response:
                    receipt = json.load(response)
                self.reply(200, {"order_id": oid, "scope": "test", "receipt": receipt})
            except HTTPError as error:
                try:
                    message = json.load(error).get("detail", "服务端回传被拒绝")
                except (ValueError, OSError):
                    message = "服务端回传被拒绝"
                self.reply(error.code, {"detail": message})
            except (ValueError, KeyError, TypeError) as error:
                self.reply(422, {"detail": str(error)})
            except OSError:
                self.reply(503, {"detail": "本地支付测试后端或采集服务不可用"})

    print(f"Pixel Playground: {origin}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--config", type=Path)
    args = parser.parse_args()
    run(args.port, args.config)
