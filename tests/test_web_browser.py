"""Focused browser regressions. Run with python -m unittest discover -s tests -p test_web_browser.py."""
import http.server
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHROME = next((shutil.which(name) for name in ("google-chrome", "chromium", "chromium-browser")
               if shutil.which(name)), None)


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def do_GET(self):
        pages = {
            "/sidebar-test": ("index.html", "web_sidebar_browser.js", '<script src="/web/app.js"></script>'),
            "/config-test": ("config.html", "web_config_browser.js", "<script>"),
        }
        if self.path not in pages:
            if self.path in {
                "/web/app.js", "/web/style.css",
                "/tests/web_sidebar_browser.js", "/tests/web_config_browser.js",
            }:
                return super().do_GET()
            self.send_error(404)
            return
        page, script, marker = pages[self.path]
        content = (ROOT / "web" / page).read_text(encoding="utf-8")
        content = content.replace(marker, f'<script src="/tests/{script}"></script>\n{marker}', 1)
        body = content.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


@unittest.skipUnless(CHROME, "Chrome or Chromium is required for browser regressions")
class WebBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def run_page(self, path):
        with tempfile.TemporaryDirectory(prefix="lean-dm-browser-") as temp_dir:
            work = Path(temp_dir)
            env = {**os.environ, "HOME": str(work), "TMPDIR": str(work),
                   "XDG_CONFIG_HOME": str(work), "XDG_CACHE_HOME": str(work)}
            result = subprocess.run(
                [CHROME, "--headless", "--no-sandbox", "--disable-gpu",
                 "--disable-dev-shm-usage", "--disable-background-networking", "--no-first-run",
                 f"--user-data-dir={work / 'profile'}", "--virtual-time-budget=3000",
                 "--dump-dom", f"http://127.0.0.1:{self.server.server_port}/{path}"],
                capture_output=True, text=True, timeout=30, env=env,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('data-test-result="PASS"', result.stdout, result.stdout)

    def test_sidebar_edits_refresh_and_actions(self):
        self.run_page("sidebar-test")

    def test_settings_delegated_actions(self):
        self.run_page("config-test")


if __name__ == "__main__":
    unittest.main()
