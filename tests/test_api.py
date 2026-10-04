import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from helpers import INCIDENT, Clock, build_midautumn_scenario, make_service
from overflow_evidence.api import make_server


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.service = make_service(cls.tmp.name, Clock())
        cls.server = make_server(cls.service, port=0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def call(self, method, path, body=None, expect=200):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req) as resp:
                self.assertEqual(resp.status, expect)
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            self.assertEqual(exc.code, expect, payload)
            return json.loads(payload)

    def test_api_flow(self):
        self.call("POST", "/incidents", {"incident_id": INCIDENT, "title": "中秋冒溢", "actor": "管理方"})
        evidence = self.call("POST", f"/incidents/{INCIDENT}/evidence", {
            "kind": "sensor", "observed_at": "2026-09-29T08:00:00+00:00", "location_code": "BLOCK-2",
            "payload": {"parameter": "cod", "value": 45.0, "threshold": 30.0},
            "source": "排水公司", "registered_by": "平台", "evidence_id": "E-API-1",
        })
        self.assertEqual(evidence["status"], "confirmed")

        # 补录：无理由 → 400；双人确认后生效
        backfill = self.call("POST", f"/incidents/{INCIDENT}/evidence", {
            "kind": "inspection", "observed_at": "2026-09-27T08:00:00+00:00", "location_code": "BLOCK-2",
            "payload": {"overflow_observed": True}, "source": "环卫", "registered_by": "甲",
            "backfill_reason": "纸质记录后补", "evidence_id": "E-API-2",
        })
        self.assertEqual(backfill["status"], "pending")
        self.call("POST", "/incidents/x/evidence", {"kind": "sensor"}, expect=404)
        self.call("POST", f"/evidence/{backfill['evidence_id']}/confirm", {"confirmer": "乙"})
        confirmed = self.call("POST", f"/evidence/{backfill['evidence_id']}/confirm", {"confirmer": "丙"})
        self.assertEqual(confirmed["status"], "confirmed")

        report = self.call("POST", f"/incidents/{INCIDENT}/reports", {"generated_by": "复盘组"})
        self.assertTrue(self.call("GET", f"/reports/{report['report_id']}/verify")["valid"])

        provenance = self.call("GET", f"/reports/{report['report_id']}/provenance?ref=zone:ZONE-BLOCK-2")
        self.assertEqual(len(provenance["materials"]), 2)

        items = self.call("GET", f"/incidents/{INCIDENT}/remediation")
        self.assertTrue(all("completion_conditions" in i for i in items))

        timeline = self.call("GET", f"/incidents/{INCIDENT}/timeline")
        self.assertTrue(all("hash" in e for e in timeline["events"]))

        self.call("POST", f"/evidence/{backfill['evidence_id']}/withdraw",
                  {"reason": "与台账不符", "withdrawn_by": "复盘组"})
        report2 = self.call("POST", f"/incidents/{INCIDENT}/reports", {"generated_by": "复盘组"})
        diff = self.call("GET", f"/incidents/{INCIDENT}/reports/diff?a={report['report_id']}&b={report2['report_id']}")
        self.assertEqual(diff["evidence"]["removed"], [backfill["evidence_id"]])

        self.assertTrue(self.call("GET", "/chain/verify")["valid"])
        self.call("GET", "/reports/RPT-NOPE", expect=404)
        error = self.call("POST", f"/incidents/{INCIDENT}/evidence", {"kind": "sensor"}, expect=400)
        self.assertEqual(error["error"], "ValidationError")


if __name__ == "__main__":
    unittest.main()
