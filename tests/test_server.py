"""python -m unittest discover tests"""
import contextlib
import http.client
import io
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import quote, urlparse

os.environ.setdefault("RETAKE_HOME", tempfile.mkdtemp(prefix="retake-test-"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "retake"))
import app  # noqa: E402
import projects  # noqa: E402


class Addresses(unittest.TestCase):
    """Requests as a browser sends them: names in Russian arrive percent-encoded."""

    @classmethod
    def setUpClass(cls):
        cls.srv, url = app.start_server(port=0)
        cls.port = urlparse(url).port
        cls.token = json.loads(re.search(r"window\.RETAKE = (\{.*?\});", cls.get("/")[1])[1])["token"]
        cls.work = tempfile.TemporaryDirectory()
        src = Path(cls.work.name) / "Японский урок ёж.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=160x90:r=10:d=1",
                        "-pix_fmt", "yuv420p", str(src)], check=True)
        cls.project = projects.create(src)
        (cls.project.dir / "edits.json").write_text(json.dumps(
            {"source": str(src), "range": [0.0, 1.0], "words": [], "phrases": [], "edits": []}))

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.work.cleanup()

    @classmethod
    def get(cls, path: str, token: str | None = None) -> tuple[int, str]:
        c = http.client.HTTPConnection("127.0.0.1", cls.port, timeout=10)
        c.request("GET", path, headers={"X-Retake-Token": token} if token else {})
        r = c.getresponse()
        body = r.read().decode("utf-8", "replace")
        c.close()
        return r.status, body

    def test_a_lesson_named_in_russian_opens(self):
        pid = quote(self.project.id)
        self.assertIn("%D0", pid)
        self.assertEqual(self.get(f"/p/{pid}")[0], 200)
        self.assertEqual(self.get(f"/api/projects/{pid}/review", self.token)[0], 200)
        self.assertEqual(self.get(f"/media/{pid}", self.token)[0], 200)

    def test_encoding_does_not_get_past_the_token(self):
        for path in ("/api/state", "/%61pi/state", "/%2561pi/state", "/media/x", "/%6Dedia/x"):
            self.assertIn(self.get(path)[0], (403, 404), path)
        self.assertEqual(self.get("/%61pi/state")[0], 403)
        self.assertEqual(self.get("/%61pi/state", self.token)[0], 200)

    def test_encoding_does_not_leave_the_ui_folder(self):
        for path in ("/ui/..%2f..%2fconfig.py", "/ui/%2e%2e/%2e%2e/config.py", "/ui/js/../../app.py"):
            self.assertEqual(self.get(path)[0], 404, path)
        self.assertEqual(self.get("/ui/js/model.js")[0], 200)


class Server(unittest.TestCase):
    def test_start_does_not_look_up_the_host_name(self):
        # without working DNS this lookup hung for 35 s on a Mac, at every launch
        with mock.patch.object(socket, "getfqdn", side_effect=AssertionError("looked up")):
            srv, url = app.start_server(port=0)
        srv.shutdown()
        self.assertTrue(url.startswith("http://127.0.0.1:"))

    def report(self, error) -> str:
        srv = app.Server(("127.0.0.1", 0), app.Handler)
        self.addCleanup(srv.server_close)
        out = io.StringIO()
        with contextlib.redirect_stderr(out):
            try:
                raise error
            except Exception:  # noqa: BLE001
                srv.handle_error(None, ("127.0.0.1", 1))
        return out.getvalue()

    def test_dropped_connections_stay_out_of_the_log(self):
        self.assertEqual(self.report(ConnectionResetError()), "")
        self.assertEqual(self.report(BrokenPipeError()), "")

    def test_real_errors_reach_the_log(self):
        self.assertIn("ValueError", self.report(ValueError("настоящая ошибка")))


if __name__ == "__main__":
    unittest.main()
