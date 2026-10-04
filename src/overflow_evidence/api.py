"""HTTP 接口（标准库实现）：与命令行等价的查询与登记能力。

路由一览：
  POST /incidents                              建立事件
  GET  /incidents                              事件列表
  POST /incidents/<id>/evidence                登记证据（可带补录信息）
  POST /incidents/<id>/import                  批量导入
  GET  /incidents/<id>/timeline                可信时间线
  GET  /incidents/<id>/conclusions             当前结论（实时推导）
  GET  /incidents/<id>/remediations            整改完成条件列表
  POST /incidents/<id>/reports                 签发新报告
  GET  /incidents/<id>/reports                 报告列表
  POST /evidence/<id>/withdraw                 撤回证据
  GET  /reports/<id>                           报告全文
  GET  /reports/<id>/verify                    校验报告摘要
  GET  /reports/<id>/evidence?ref=...          结论所依据的材料
  GET  /reports/diff?old=...&new=...           历次报告差异
  GET  /remediations/<task_id>/completion      整改完成条件
  POST /remediations/<task_id>/reinspections   登记复验结果
  POST /escalations/check                      逾期升级检查
"""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .ledger import LedgerError
from .remediation import RemediationError
from .reports import ReportError
from .service import IncidentService


def make_handler(service: IncidentService) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "OverflowEvidence/1.0"

        # ---- 基础工具 ----
        def _send(self, code: int, obj) -> None:
            body = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _error(self, code: int, message: str) -> None:
            self._send(code, {"error": message})

        def _body(self):
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            try:
                return json.loads(self.rfile.read(length).decode("utf-8"))
            except json.JSONDecodeError as exc:
                raise ValueError(f"请求体不是合法 JSON: {exc}") from exc

        def log_message(self, *args) -> None:  # 静默
            pass

        # ---- 路由 ----
        def do_GET(self) -> None:  # noqa: N802
            try:
                self._route_get(urlparse(self.path))
            except (LedgerError, RemediationError, ReportError, KeyError, ValueError) as exc:
                self._error(404 if isinstance(exc, KeyError) else 400, str(exc))

        def do_POST(self) -> None:  # noqa: N802
            try:
                self._route_post(urlparse(self.path))
            except (LedgerError, RemediationError, ReportError, KeyError, ValueError) as exc:
                self._error(404 if isinstance(exc, KeyError) else 400, str(exc))

        def _route_get(self, url) -> None:
            parts = [p for p in url.path.split("/") if p]
            q = parse_qs(url.query)
            if parts == ["incidents"]:
                return self._send(200, service.incidents())
            if len(parts) == 3 and parts[0] == "incidents" and parts[2] == "timeline":
                return self._send(200, service.timeline(parts[1]))
            if len(parts) == 3 and parts[0] == "incidents" and parts[2] == "conclusions":
                return self._send(200, service.conclusions(parts[1]).to_dict())
            if len(parts) == 3 and parts[0] == "incidents" and parts[2] == "remediations":
                return self._send(200, service.remediations_for(parts[1]))
            if len(parts) == 3 and parts[0] == "incidents" and parts[2] == "reports":
                return self._send(200, service.list_reports(parts[1]))
            if parts == ["reports", "diff"]:
                return self._send(200, service.diff_reports(q["old"][0], q["new"][0]))
            if len(parts) == 2 and parts[0] == "reports":
                return self._send(200, service.get_report(parts[1]).to_dict())
            if len(parts) == 3 and parts[0] == "reports" and parts[2] == "verify":
                return self._send(200, service.verify_report(parts[1]))
            if len(parts) == 3 and parts[0] == "reports" and parts[2] == "evidence":
                ref = q.get("ref", ["report"])[0]
                return self._send(200, service.conclusion_evidence(parts[1], ref))
            if len(parts) == 3 and parts[0] == "remediations" and parts[2] == "completion":
                return self._send(200, service.remediation_completion(parts[1]))
            return self._error(404, f"未知路由 GET {url.path}")

        def _route_post(self, url) -> None:
            parts = [p for p in url.path.split("/") if p]
            body = self._body()
            if parts == ["incidents"]:
                out = service.create_incident(
                    body["incident_id"], body["opened_at"], body.get("description", ""))
                return self._send(201, out)
            if len(parts) == 3 and parts[0] == "incidents" and parts[2] == "evidence":
                rec = service.submit_evidence(
                    parts[1],
                    kind=body["kind"],
                    observed_at=body["observed_at"],
                    location_code=body["location_code"],
                    payload=body["payload"],
                    evidence_id=body.get("evidence_id"),
                    source=body.get("source", "api"),
                    backfill_reason=body.get("backfill_reason"),
                    confirmers=body.get("confirmers") or (),
                )
                return self._send(201, rec.to_dict())
            if len(parts) == 3 and parts[0] == "incidents" and parts[2] == "import":
                return self._send(200, service.import_batch(parts[1], body["records"]).to_dict())
            if len(parts) == 3 and parts[0] == "incidents" and parts[2] == "reports":
                return self._send(201, service.generate_report(parts[1]).summary())
            if len(parts) == 3 and parts[0] == "evidence" and parts[2] == "withdraw":
                w = service.withdraw_evidence(
                    parts[1], reason=body["reason"], withdrawn_by=body["withdrawn_by"])
                return self._send(200, w.to_dict())
            if len(parts) == 3 and parts[0] == "remediations" and parts[2] == "reinspections":
                rec = service.record_reinspection(
                    parts[1],
                    evidence_id=body["evidence_id"],
                    passed=bool(body["passed"]),
                    inspector=body["inspector"],
                    note=body.get("note", ""),
                )
                return self._send(201, rec)
            if parts == ["escalations", "check"]:
                return self._send(200, service.check_overdue(body.get("now")))
            return self._error(404, f"未知路由 POST {url.path}")

    return Handler


def serve(service: IncidentService, host: str = "127.0.0.1", port: int = 8080) -> None:
    httpd = ThreadingHTTPServer((host, port), make_handler(service))
    print(f"溢流事件管理服务监听 http://{host}:{port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
