"""马拉松赛务判定中枢的运行入口。"""

import argparse
import json
from http.server import ThreadingHTTPServer
from pathlib import Path

from adjudication.api import (SERVICE_ID, SERVICE_NAME, App, health_payload,
                              make_handler)
from adjudication.journal import Journal
from adjudication.rules import default_rules

__all__ = ["SERVICE_ID", "SERVICE_NAME", "health_payload", "build_app", "main"]


def load_fixture():
    path = Path(__file__).with_name("fixtures") / "sample.json"
    return json.loads(path.read_text(encoding="utf-8"))


def build_app(data_path=None):
    """按赛事参数装配应用：默认规则版本 + 事件日志 + 医疗资源台账。"""
    fixture = load_fixture()
    rules = default_rules(fixture)
    journal = Journal(path=data_path)
    return App(rules, journal=journal, medical_cfg=fixture["medical"])


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--data", default=None, help="事件日志持久化文件(JSONL)")
    args = parser.parse_args()
    if args.check:
        app = build_app()
        assert health_payload()["service"] == SERVICE_ID
        app.state()  # 折叠一次事件日志，验证规则与投影可用
        print("基础检查通过")
        return
    app = build_app(data_path=args.data)
    ThreadingHTTPServer(("0.0.0.0", args.port), make_handler(app)).serve_forever()


if __name__ == "__main__":
    main()
