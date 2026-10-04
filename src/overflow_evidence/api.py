"""HTTP 接口（仅标准库）：与命令行共用同一服务层。

路由概览：
    POST /incidents                                登记事件
    GET  /incidents/{iid}/timeline                 可信时间线
    GET  /incidents/{iid}/conclusions              当前结论
    POST /incidents/{iid}/evidence                 登记证据
    POST /incidents/{iid}/import                   批量导入
    POST /incidents/{iid}/reports                  签发报告
    GET  /incidents/{iid}/reports                  历次报告
    GET  /incidents/{iid}/reports/diff?a=&b=       报告差异
    GET  /incidents/{iid}/remediation[/{item}]     整改项与完成条件
    POST /incidents/{iid}/remediation/{item}/reinspections  登记复验
    GET  /incidents/{iid}/escalations              逾期升级
    POST /evidence/{eid}/confirm                   补录确认
    POST /evidence/{eid}/withdraw                  撤回证据
    GET  /reports/{rid}                            报告全文
    GET  /reports/{rid}/verify                     校验摘要
    GET  /reports/{rid}/provenance?ref=            结论依据材料
    GET  /chain/verify                             校验事件哈希链
"""
from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .errors import ConflictError, NotFoundError, OverflowError, ValidationError
from .service import OverflowService

STATUS_BY_ERROR = {ValidationError: 400, NotFoundError: 404, ConflictError: 409}

Route = tuple[str, re.Pattern, str]


def _routes() -> list[Route]:
    def rx(pattern: str) -> re.Pattern:
        return re.compile(f"^{pattern}$")

    return [
        ("POST", rx(r"/incidents"), "create_incident"),
        ("GET", rx(r"/incidents/(?P<iid>[^/]+)/timeline"), "timeline"),
        ("GET", rx(r"/incidents/(?P<iid>[^/]+)/conclusions"), "conclusions"),
        ("POST", rx(r"/incidents/(?P<iid>[^/]+)/evidence"), "register_evidence"),
        ("POST", rx(r"/incidents/(?P<iid>[^/]+)/import"), "import_batch"),
        ("POST", rx(r"/incidents/(?P<iid>[^/]+)/reports"), "issue_report"),
        ("GET", rx(r"/incidents/(?P<iid>[^/]+)/reports"), "list_reports"),
        ("GET", rx(r"/incidents/(?P<iid>[^/]+)/reports/diff"), "diff_reports"),
        ("GET", rx(r"/incidents/(?P<iid>[^/]+)/remediation"), "remediation"),
        ("GET", rx(r"/incidents/(?P<iid>[^/]+)/remediation/(?P<item>[^/]+)"), "remediation_item"),
        ("POST", rx(r"/incidents/(?P<iid>[^/]+)/remediation/(?P<item>[^/]+)/reinspections"), "reinspection"),
        ("GET", rx(r"/incidents/(?P<iid>[^/]+)/escalations"), "escalations"),
        ("POST", rx(r"/evidence/(?P<eid>[^/]+)/confirm"), "confirm_evidence"),
        ("POST", rx(r"/evidence/(?P<eid>[^/]+)/withdraw"), "withdraw_evidence"),
        ("GET", rx(r"/evidence/(?P<eid>[^/]+)"), "get_evidence"),
        ("GET", rx(r"/reports/(?P<rid>[^/]+)"), "get_report"),
        ("GET", rx(r"/reports/(?P<rid>[^/]+)/verify"), "verify_report"),
        ("GET", rx(r"/reports/(?P<rid>[^/]+)/provenance"), "provenance"),
        ("GET", rx(r"/chain/verify"), "verify_chain"),
        ("GET", rx(r"/health"), "health"),
    ]


class ApiHandler(BaseHTTPRequestHandler):
    service: OverflowService = None  # 由 serve() 注入
    server_version = "OverflowEvidence/1.0"

    # ------------------------------------------------------------------ helpers
    def _send(self, status: int, payload) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValidationError(f"请求体不是合法 JSON：{exc}") from exc

    def log_message(self, fmt, *args):  # 静默访问日志
        pass

    # ------------------------------------------------------------------ dispatch
    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        try:
            for route_method, pattern, action in _routes():
                if route_method != method:
                    continue
                match = pattern.match(parsed.path)
                if match:
                    result = self._handle(action, match.groupdict(), query)
                    self._send(200, result)
                    return
            self._send(404, {"error": "NotFound", "message": f"无此路由：{method} {parsed.path}"})
        except OverflowError as exc:
            status = next((code for klass, code in STATUS_BY_ERROR.items() if isinstance(exc, klass)), 500)
            self._send(status, {"error": type(exc).__name__, "message": str(exc)})
        except Exception as exc:  # pragma: no cover
            self._send(500, {"error": "InternalError", "message": str(exc)})

    def _handle(self, action: str, path: dict, query: dict):
        svc = self.service
        body = self._body() if self.command == "POST" else {}

        if action == "health":
            return {"ok": True}
        if action == "create_incident":
            return svc.create_incident(
                body.get("incident_id", ""),
                body.get("title", ""),
                body.get("description", ""),
                actor=body.get("actor", "api"),
            )
        if action == "timeline":
            return svc.timeline(path["iid"])
        if action == "conclusions":
            return svc.conclusions(path["iid"])
        if action == "register_evidence":
            return svc.register_evidence(
                path["iid"],
                kind=body.get("kind", ""),
                observed_at=body.get("observed_at", ""),
                location_code=body.get("location_code", ""),
                payload=body.get("payload", {}),
                source=body.get("source", ""),
                registered_by=body.get("registered_by", ""),
                evidence_id=body.get("evidence_id"),
                is_backfill=bool(body.get("is_backfill")),
                backfill_reason=body.get("backfill_reason"),
            )
        if action == "import_batch":
            return svc.import_batch(
                path["iid"],
                body.get("records", []),
                mode=body.get("mode", "partial"),
                actor=body.get("actor", "api-import"),
            )
        if action == "issue_report":
            return svc.issue_report(path["iid"], body.get("generated_by", "api"))
        if action == "list_reports":
            return svc.list_reports(path["iid"])
        if action == "diff_reports":
            return svc.diff_reports(path["iid"], _first(query, "a"), _first(query, "b"))
        if action == "remediation":
            return svc.remediation(path["iid"])
        if action == "remediation_item":
            return svc.remediation(path["iid"], path["item"])
        if action == "reinspection":
            return svc.record_reinspection(
                path["iid"],
                path["item"],
                result=body.get("result", ""),
                inspector=body.get("inspector", ""),
                note=body.get("note", ""),
                evidence_id=body.get("evidence_id"),
            )
        if action == "escalations":
            return svc.escalations(path["iid"])
        if action == "confirm_evidence":
            return svc.confirm_evidence(path["eid"], body.get("confirmer", ""))
        if action == "withdraw_evidence":
            return svc.withdraw_evidence(path["eid"], body.get("reason", ""), body.get("withdrawn_by", ""))
        if action == "get_evidence":
            return svc.get_evidence(path["eid"])
        if action == "get_report":
            return svc.get_report(path["rid"])
        if action == "verify_report":
            return svc.verify_report(path["rid"])
        if action == "provenance":
            return svc.conclusion_evidence(path["rid"], _first(query, "ref"))
        if action == "verify_chain":
            return svc.verify_chain()
        raise NotFoundError(f"未知操作：{action}")  # pragma: no cover


def _first(query: dict, key: str) -> str:
    values = query.get(key)
    if not values:
        raise ValidationError(f"缺少查询参数：{key}")
    return values[0]


def make_server(service: OverflowService, host: str = "127.0.0.1", port: int = 8080) -> ThreadingHTTPServer:
    handler = type("BoundApiHandler", (ApiHandler,), {"service": service})
    return ThreadingHTTPServer((host, port), handler)


def serve(service: OverflowService, host: str = "127.0.0.1", port: int = 8080) -> None:
    server = make_server(service, host, port)
    print(f"HTTP 接口已启动：http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:  # pragma: no cover
        pass
    finally:
        server.server_close()


if __name__ == "__main__":  # pragma: no cover
    import sys

    data_dir = sys.argv[1] if len(sys.argv) > 1 else ".overflow_data"
    serve(OverflowService(data_dir))
