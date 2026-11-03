import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from jobagent.fetch import FetchError, fetch_jd_text, html_to_text

PAGE = """<html><head><title>Job</title><style>.x{color:red}</style>
<script>var secret = "SCRIPT_TEXT";</script></head>
<body><h1>Frontend Engineer</h1><p>Build   UIs
with React.</p><ul><li>React</li><li>TypeScript</li></ul><noscript>NOSCRIPT_TEXT</noscript></body></html>"""


@pytest.fixture
def server():
    state = {"status": 200, "body": PAGE, "agents": []}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state["agents"].append(self.headers.get("User-Agent"))
            payload = state["body"].encode("utf-8")
            self.send_response(state["status"])
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}/job", state
    httpd.shutdown()
    httpd.server_close()


def test_html_to_text_keeps_only_visible_text_one_block_per_line():
    assert html_to_text(PAGE).split("\n") == [
        "Job", "Frontend Engineer", "Build UIs with React.", "React", "TypeScript",
    ]


def test_fetch_returns_the_visible_text_of_a_page_and_identifies_itself(server):
    url, state = server

    text = fetch_jd_text(url)

    assert "Frontend Engineer" in text
    assert "SCRIPT_TEXT" not in text and "NOSCRIPT_TEXT" not in text
    assert state["agents"][0].startswith("jobagent/")


def test_an_http_error_status_is_reported(server):
    url, state = server
    state["status"] = 404

    with pytest.raises(FetchError, match="404"):
        fetch_jd_text(url)


def test_a_page_with_no_readable_text_points_at_the_text_file_option(server):
    url, state = server
    state["body"] = "<html><body><div id='root'></div><script>app()</script></body></html>"

    with pytest.raises(FetchError, match="--text-file"):
        fetch_jd_text(url)


def test_an_unreachable_site_is_reported():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    with pytest.raises(FetchError, match="cannot reach"):
        fetch_jd_text(f"http://127.0.0.1:{port}/job")


def test_the_text_is_capped(server):
    url, state = server
    state["body"] = "<p>" + "word " * 5000 + "</p>"

    assert len(fetch_jd_text(url, max_chars=100)) <= 100


@pytest.mark.parametrize("url", ["file:///C:/Windows/win.ini", "ftp://example.com/job", "javascript:alert(1)"])
def test_only_http_and_https_addresses_are_fetched(url):
    with pytest.raises(FetchError, match="http"):
        fetch_jd_text(url)
