import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
import yaml

from jobagent.cli import main
from jobagent.storage import Storage

FIXTURES = Path(__file__).resolve().parent / "fixtures"

PAGES = {
    "/jobs/1": "<html><head><title>Frontend Engineer at Acme</title></head><body><h1>Frontend Engineer</h1>"
    "<p>Acme builds dashboards. React and TypeScript required, GraphQL a plus.</p></body></html>",
    "/jobs/2": "<html><body><h1>Backend Engineer</h1><p>Acme needs Go and Postgres experience.</p></body></html>",
}

EXTRACTIONS = {
    "Frontend": {
        "title": "Frontend Engineer",
        "company": "Acme",
        "must_have_skills": ["React", "TypeScript"],
        "nice_to_have_skills": ["GraphQL"],
    },
    "Backend": {"title": "Backend Engineer", "company": "Acme", "must_have_skills": ["Go"], "nice_to_have_skills": []},
}
SCORING = {
    "must_have": [{"skill": "React", "match": "full"}, {"skill": "TypeScript", "match": "full"}],
    "nice_to_have": [{"skill": "GraphQL", "match": "none"}],
    "reason": "Strong React and TypeScript.",
}


@pytest.fixture
def site():
    model_calls = []

    class Handler(BaseHTTPRequestHandler):
        def _send(self, status, body, content_type):
            payload = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            if self.path in PAGES:
                self._send(200, PAGES[self.path], "text/html; charset=utf-8")
            else:
                self._send(404, "not found", "text/plain")

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            system, user = request["messages"][0]["content"], request["messages"][1]["content"]
            if "extract structured data" in system:
                kind, reply = "extraction", json.dumps(EXTRACTIONS["Backend" if "Backend" in user else "Frontend"])
            elif "compare the skills" in system:
                kind, reply = "scoring", json.dumps(SCORING)
            else:
                kind, reply = "summary", "A frontend role at Acme building dashboards."
            model_calls.append(kind)
            body = json.dumps({"choices": [{"message": {"content": reply}}]})
            self._send(200, body, "application/json")

        def log_message(self, *args):
            pass

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}", model_calls
    httpd.shutdown()
    httpd.server_close()


def test_a_batch_runs_through_real_http_fetching_the_model_client_the_matcher_and_the_tracker(site, tmp_path, capsys):
    base, model_calls = site
    settings = yaml.safe_load((FIXTURES / "settings.yaml").read_text(encoding="utf-8"))
    settings["llm"]["provider"] = "local"
    settings["llm"]["providers"]["local"]["base_url"] = f"{base}/v1"
    prefs = yaml.safe_load((FIXTURES / "preferences.yaml").read_text(encoding="utf-8"))
    prefs["target"]["roles"] = ["Frontend Engineer"]
    (tmp_path / "settings.yaml").write_text(yaml.safe_dump(settings), encoding="utf-8")
    (tmp_path / "preferences.yaml").write_text(yaml.safe_dump(prefs), encoding="utf-8")
    urls = tmp_path / "urls.txt"
    urls.write_text("\n".join(f"{base}/jobs/{n}" for n in (1, 2, 3)), encoding="utf-8")

    code = main(
        [
            "batch", str(urls),
            "--settings", str(tmp_path / "settings.yaml"),
            "--preferences", str(tmp_path / "preferences.yaml"),
            "--profile-dir", str(FIXTURES / "profile"),
            "--db", str(tmp_path / "jobs.db"),
        ],
        env={},
    )

    out = capsys.readouterr().out
    assert code == 1
    assert "Done: 1 apply, 0 watch_later, 1 reject, 0 skipped, 1 errors." in out
    assert "[3/3] ERROR" in out and "404" in out

    storage = Storage(tmp_path / "jobs.db")
    frontend, backend = storage.list_jobs()
    assert (frontend.status, frontend.score, frontend.title) == ("queued", 85.7, "Frontend Engineer")
    assert frontend.jd_summary == "A frontend role at Acme building dashboards."
    assert backend.status == "rejected"
    assert "target roles" in storage.list_evaluations(backend.id)[0].reason
    assert storage.get_job_by_url(f"{base}/jobs/3") is None
    assert model_calls == ["extraction", "scoring", "summary", "extraction"]
