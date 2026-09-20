"""马拉松赛务判定中枢运行入口。

- ``python3 service.py --check``：自检身份与依赖
- ``python3 service.py --replay``：在内存中重演四类演练事件并打印判定摘要
- ``python3 service.py --seed-scenario --store data/events.jsonl``：
  把演练事件落库后启动（或直接启动，POST /v1/events 自行上报）
- ``python3 service.py --port 8000``：启动 HTTP 服务
"""

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from app.api import ApiApp
from app.events import EventStore

SERVICE_ID = "marathon-adjudication"
SERVICE_NAME = "马拉松赛务判定中枢"


def health_payload():
    """返回稳定的服务身份信息。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


def build_app(store_path=None):
    store = EventStore(store_path)
    return ApiApp(store)


class Handler(BaseHTTPRequestHandler):
    app = build_app()

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        return self.rfile.read(length) if length else b""

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method):
        status, payload = self.app.handle(
            method, self.path, self._lower_headers(), self._read_body())
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _lower_headers(self):
        return {k.lower(): v for k, v in self.headers.items()}

    def log_message(self, *_args):
        return


# --------------------------------------------------------------------------
# 重演摘要
# --------------------------------------------------------------------------

def replay_summary():
    """灌入演练事件并输出关键判定，供人工核对四类情形。"""
    from app import scenarios
    app = build_app()
    receipts = scenarios.load(app)

    with app.store.lock():
        state = app.state.rebuild(app.store.canonical_events())
        bulletin = state.latest_bulletin()

        lines = []
        lines.append(f"事件入库 {len(receipts)} 条（重放去重后 "
                     f"{app.store.count()} 条）")
        lines.append("")

        lines.append("== 名次（按枪成绩，男女分组）==")
        from app.scoring import compute_rankings, compute_awards
        rows = compute_rankings(state, bulletin, "marathon")
        for row in rows:
            tag = "  [DQ:{}]".format(",".join(row["dq_reasons"])) if row["disqualified"] else ""
            gun = row["official"].get("gun_seconds")
            lines.append(
                f"  {row['gender']} #{str(row.get('overall_place')):>3} {row['bib']} "
                f"{row['name']} {row['nationality']} 枪:{gun:.0f}s "
                f"中国籍#{row.get('domestic_place')}{tag}")

        lines.append("")
        lines.append("== 奖励（v1 公报）==")
        for item in compute_awards(state, bulletin, "marathon"):
            lines.append(f"  {item['bib']} {item['award']} "
                         f"名次={item.get('place', '-')} 奖金={item.get('prize')}")

        lines.append("")
        lines.append("== 关键违规 ==")
        from app.scoring import evaluate_bib
        for bib in ("A005", "A006", "A008", "A007"):
            res = evaluate_bib(state, bib, bulletin)
            codes = [i["code"] for i in res["irregularities"]]
            lines.append(f"  {bib}: status={res['status']} "
                         f"DQ={res['disqualified']} {sorted(set(codes))}")

        lines.append("")
        lines.append("== 医疗案件 CASE-7001（指挥视角）==")
        from app.medical import case_view, nearest_available, cases_for_role
        view = case_view(state, "CASE-7001", "medical_commander")
        lines.append(f"  假名={view['runner_pseudonym']} bib_link={view['bib_link']}")
        lines.append(f"  锁定资源={view['locked_resources']}")
        for tr in view["transports"]:
            lines.append(f"  转运: {tr.get('ambulance_id')} -> "
                         f"{tr.get('hospital_id')} ({tr['kind']})")
        lines.append(f"  站点岗位可见案件数="
                     f"{len([c for c in cases_for_role(state, 'station:STA-14') if c])}")
        lines.append(f"  无关站点可见案件数="
                     f"{len([c for c in cases_for_role(state, 'station:STA-16') if c])}")
        return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--store", default=None, help="事件 JSONL 持久化路径")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--replay", action="store_true",
                        help="重演演练事件并打印判定摘要")
    parser.add_argument("--seed-scenario", action="store_true",
                        help="启动前把演练事件写入（空）存储")
    args = parser.parse_args()

    if args.check:
        assert health_payload()["service"] == SERVICE_ID
        build_app()  # 构造一次，确保模块可加载
        print("基础检查通过")
        return

    if args.replay:
        print(replay_summary())
        return

    store = EventStore(args.store)
    if args.seed_scenario:
        if store.count() == 0:
            from app import scenarios
            api_app = ApiApp(store)
            scenarios.load(api_app)
            print(f"已写入演练事件 {store.count()} 条")
        else:
            print(f"存储非空（{store.count()} 条），跳过种子写入")

    Handler.app = ApiApp(store)
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
