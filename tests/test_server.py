"""python -m unittest discover tests"""
import contextlib
import io
import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("RETAKE_HOME", tempfile.mkdtemp(prefix="retake-test-"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "retake"))
import app  # noqa: E402


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
