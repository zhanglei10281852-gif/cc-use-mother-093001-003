import json
import sys
import threading
import unittest
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from overflow_evidence.api import make_handler
from overflow_evidence.service import IncidentService

OPENED = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
RECENT = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.svc = IncidentService()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cls.svc))
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def post(self, path, body):
        req = urllib.request.Request(
            self.url(path), data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def get(self, path):
        with urllib.request.urlopen(self.url(path)) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def test_end_to_end(self):
        self.post("/incidents", {"incident_id": "INC-API", "opened_at": OPENED})
        self.post("/incidents/INC-API/evidence", {
            "kind": "sensor", "location_code": "BLOCK-2",
            "observed_at": RECENT,
            "payload": {"metric": "cod", "value": 120, "limit": 40}})
        timeline = self.get("/incidents/INC-API/timeline")
        self.assertEqual(len(timeline), 1)

        report = self.post("/incidents/INC-API/reports", {})
        rid = report["report_id"]
        self.assertTrue(self.get(f"/reports/{rid}/verify")["valid"])

        ev = self.get(f"/reports/{rid}/evidence?ref=segment:BLOCK-2")
        self.assertEqual(len(ev), 1)

        completion = self.get("/remediations/INC-API:contain:BLOCK-2/completion")
        self.assertEqual(completion["status"], "open")

        self.post("/evidence/EV-00001/withdraw",
                  {"reason": "编号有误", "withdrawn_by": "值班长"})
        report2 = self.post("/incidents/INC-API/reports", {})
        diff = self.get(f"/reports/diff?old={rid}&new={report2['report_id']}")
        self.assertEqual(diff["evidence"]["withdrawn"], ["EV-00001"])

    def test_unknown_route_404(self):
        try:
            self.get("/nope")
            self.fail("应当返回 404")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 404)


if __name__ == "__main__":
    unittest.main()
