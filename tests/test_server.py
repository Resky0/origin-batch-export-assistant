import http.client
import tempfile
import time
import unittest
from pathlib import Path

from origin_bridge.core import start_bridge_server


ALLOWED_ORIGIN = "https://layout-assistant.xyz"


class ServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name) / "selected.figgrid"
        self.project.write_bytes(b"figgrid-payload")
        self.server, self.session, self.thread = start_bridge_server(self.project)
        self.port = self.server.server_address[1]
        self.project_path = f"/v1/sessions/{self.session.token}/project"
        self.complete_path = f"/v1/sessions/{self.session.token}/complete"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def request(self, method: str, path: str, origin: str = ALLOWED_ORIGIN):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.request(method, path, headers={"Origin": origin})
        response = connection.getresponse()
        body = response.read()
        headers = dict(response.getheaders())
        connection.close()
        return response.status, headers, body

    def test_serves_project_with_restricted_cors(self) -> None:
        status, headers, body = self.request("GET", self.project_path)
        self.assertEqual(200, status)
        self.assertEqual(b"figgrid-payload", body)
        self.assertEqual(ALLOWED_ORIGIN, headers["Access-Control-Allow-Origin"])
        self.assertEqual("true", headers["Access-Control-Allow-Private-Network"])
        self.assertEqual("no-store", headers["Cache-Control"])

    def test_rejects_unknown_origin(self) -> None:
        status, _headers, _body = self.request(
            "GET", self.project_path, "https://attacker.example"
        )
        self.assertEqual(403, status)

    def test_options_supports_private_network_preflight(self) -> None:
        status, headers, _body = self.request("OPTIONS", self.project_path)
        self.assertEqual(204, status)
        self.assertEqual("true", headers["Access-Control-Allow-Private-Network"])

    def test_complete_revokes_session(self) -> None:
        status, _headers, _body = self.request("POST", self.complete_path)
        self.assertEqual(200, status)
        self.assertTrue(self.session.completed.is_set())
        status, _headers, _body = self.request("GET", self.project_path)
        self.assertEqual(410, status)

    def test_expired_session_is_gone(self) -> None:
        self.session.expires_at = time.monotonic() - 1
        status, _headers, _body = self.request("GET", self.project_path)
        self.assertEqual(410, status)


if __name__ == "__main__":
    unittest.main()
