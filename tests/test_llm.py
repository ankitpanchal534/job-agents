import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
import yaml

from jobagent.config import load_settings
from jobagent.llm import ChatClient, LLMError

SETTINGS_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "settings.yaml"


@pytest.fixture
def server():
    received = []
    state = {"status": 200, "reply": "hello", "models": ["b-model", "a-model"]}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            received.append({"path": self.path, "authorization": self.headers.get("Authorization"), "user_agent": self.headers.get("User-Agent")})
            payload = json.dumps({"data": [{"id": name} for name in state["models"]]}).encode()
            self.send_response(state["status"])
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_POST(self):
            length = int(self.headers["Content-Length"])
            received.append(
                {
                    "path": self.path,
                    "authorization": self.headers.get("Authorization"),
                    "user_agent": self.headers.get("User-Agent"),
                    "body": json.loads(self.rfile.read(length)),
                }
            )
            payload = json.dumps({"choices": [{"message": {"content": state["reply"]}}]}).encode()
            self.send_response(state["status"])
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_port}/v1", received, state
    httpd.shutdown()
    httpd.server_close()


def make_settings(tmp_path, base_url, provider="local"):
    data = yaml.safe_load(SETTINGS_FIXTURE.read_text(encoding="utf-8"))
    data["llm"]["provider"] = provider
    for name in data["llm"]["providers"]:
        data["llm"]["providers"][name]["base_url"] = base_url
    path = tmp_path / "settings.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return load_settings(path)


def test_complete_sends_the_task_model_and_settings_to_the_local_provider(server, tmp_path):
    base_url, received, _ = server
    client = ChatClient(make_settings(tmp_path, base_url), env={})

    reply = client.complete("jd_summary", "be brief", "summarise this")

    assert reply == "hello"
    request = received[0]
    assert request["path"] == "/v1/chat/completions"
    assert request["authorization"] is None
    assert request["body"]["model"] == "qwen2.5:7b"
    assert request["body"]["temperature"] == 0.2
    assert request["body"]["max_tokens"] == 400
    assert request["body"]["messages"] == [
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": "summarise this"},
    ]


def test_complete_uses_the_openai_model_and_key_when_toggled(server, tmp_path):
    base_url, received, _ = server
    client = ChatClient(make_settings(tmp_path, base_url, provider="openai"), env={"OPENAI_API_KEY": "sk-test"})

    client.complete("jd_summary", "s", "u")

    assert received[0]["authorization"] == "Bearer sk-test"
    assert received[0]["body"]["model"] == "gpt-4o-mini"


def test_missing_api_key_fails_before_any_request_is_sent(server, tmp_path):
    base_url, received, _ = server
    client = ChatClient(make_settings(tmp_path, base_url, provider="openai"), env={})

    with pytest.raises(LLMError, match="OPENAI_API_KEY"):
        client.complete("jd_summary", "s", "u")

    assert received == []


def test_http_error_is_reported_with_its_status(server, tmp_path):
    base_url, _, state = server
    state["status"] = 500
    client = ChatClient(make_settings(tmp_path, base_url), env={})

    with pytest.raises(LLMError, match="500"):
        client.complete("jd_summary", "s", "u")


def test_unreachable_provider_is_reported(tmp_path):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        closed_port = probe.getsockname()[1]
    client = ChatClient(make_settings(tmp_path, f"http://127.0.0.1:{closed_port}/v1"), env={})

    with pytest.raises(LLMError, match="cannot reach"):
        client.complete("jd_summary", "s", "u")


def test_list_models_returns_sorted_ids_from_the_provider(server, tmp_path):
    base_url, received, _ = server
    client = ChatClient(make_settings(tmp_path, base_url), env={})

    assert client.list_models() == ["a-model", "b-model"]
    assert received[0]["path"] == "/v1/models"
    assert received[0]["authorization"] is None


def test_list_models_can_target_another_provider_and_sends_its_key(server, tmp_path):
    base_url, received, _ = server
    client = ChatClient(make_settings(tmp_path, base_url), env={"OPENAI_API_KEY": "sk-test"})

    client.list_models("openai")

    assert received[0]["authorization"] == "Bearer sk-test"


def test_list_models_without_the_key_fails_before_any_request(server, tmp_path):
    base_url, received, _ = server
    client = ChatClient(make_settings(tmp_path, base_url), env={})

    with pytest.raises(LLMError, match="OPENAI_API_KEY"):
        client.list_models("openai")

    assert received == []


def test_requests_carry_a_jobagent_user_agent_because_some_providers_block_the_default_one(server, tmp_path):
    base_url, received, _ = server
    client = ChatClient(make_settings(tmp_path, base_url), env={})

    client.complete("jd_summary", "s", "u")
    client.list_models()

    assert [r["user_agent"] for r in received] == ["jobagent/0.1", "jobagent/0.1"]
