import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from fakes import Clock, FakeChannel

from jobagent.config import load_settings
from jobagent.notify import (
    DesktopChannel,
    Notification,
    Notifier,
    NotifyError,
    NtfyChannel,
    build_channels,
)
from jobagent.storage import Storage

FIXTURES = Path(__file__).resolve().parent / "fixtures"
URL = "https://boards.greenhouse.io/acme/jobs/1"


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def storage(tmp_path, clock):
    return Storage(tmp_path / "jobs.db", clock=clock)


def new_block(storage, n=1, reason="captcha", host="boards.greenhouse.io", **kwargs):
    job = storage.add_job(f"https://{host}/acme/jobs/{n}")
    storage.update_job(job.id, title="Frontend Engineer", company="Acme")
    return storage.block_job(job.id, reason, **kwargs)


def test_a_block_sends_one_alert_with_the_job_link_and_screenshot(storage):
    channel = FakeChannel()
    block = new_block(storage, detail="Cloudflare check", screenshot_path="shots/1.png")

    report = Notifier(storage, [channel]).notify_block(block)

    alert = channel.sent[0]
    assert alert.title == "Job needs you: CAPTCHA"
    assert "Frontend Engineer" in alert.message and "Acme" in alert.message and "Cloudflare check" in alert.message
    assert alert.url == "https://boards.greenhouse.io/acme/jobs/1"
    assert alert.attachment == "shots/1.png" and alert.priority == "high"
    assert report.sent == ["fake"] and not report.suppressed
    assert storage.list_blocks()[0].notified_at is not None


def test_a_second_alert_for_the_same_problem_on_the_same_site_is_held_back_then_summarised(storage, clock):
    channel = FakeChannel()
    notifier = Notifier(storage, [channel], cooldown_minutes=15, clock=clock)
    notifier.notify_block(new_block(storage, 1))
    clock.advance(1)

    held = notifier.notify_block(new_block(storage, 2))

    assert held.suppressed and held.sent == [] and len(channel.sent) == 1

    clock.advance(20)
    later = notifier.notify_block(new_block(storage, 3))
    assert later.sent == ["fake"] and len(channel.sent) == 2
    assert "+1 similar" in channel.sent[1].message


def test_a_different_problem_or_a_different_site_still_alerts(storage, clock):
    channel = FakeChannel()
    notifier = Notifier(storage, [channel], cooldown_minutes=15, clock=clock)
    notifier.notify_block(new_block(storage, 1))

    notifier.notify_block(new_block(storage, 2, reason="login_expired"))
    notifier.notify_block(new_block(storage, 3, host="jobs.lever.co"))

    assert len(channel.sent) == 3


def test_a_zero_cooldown_never_holds_alerts_back(storage, clock):
    channel = FakeChannel()
    notifier = Notifier(storage, [channel], cooldown_minutes=0, clock=clock)

    notifier.notify_block(new_block(storage, 1))
    notifier.notify_block(new_block(storage, 2))

    assert len(channel.sent) == 2


def test_one_failing_channel_does_not_stop_the_others_or_raise(storage):
    good = FakeChannel("good")
    block = new_block(storage)

    report = Notifier(storage, [FakeChannel("bad", fail=True), good]).notify_block(block)

    assert report.sent == ["good"] and "bad is down" in report.failed["bad"]
    assert len(good.sent) == 1
    assert storage.list_blocks()[0].notified_at is not None


def test_when_every_channel_fails_nothing_is_marked_so_the_next_alert_is_not_held_back(storage, clock):
    notifier = Notifier(storage, [FakeChannel("bad", fail=True)], clock=clock)
    first = notifier.notify_block(new_block(storage, 1))
    assert first.sent == [] and "bad" in first.failed
    assert storage.list_blocks()[0].notified_at is None

    ok = FakeChannel()
    second = Notifier(storage, [ok], clock=clock).notify_block(new_block(storage, 2))

    assert second.sent == ["fake"] and not second.suppressed


def settings_with(tmp_path, ntfy_topic, enabled=True, toast=True, sound=True):
    data = yaml.safe_load((FIXTURES / "settings.yaml").read_text(encoding="utf-8"))
    data["notifications"] = {
        "ntfy": {"enabled": enabled, "server": "https://ntfy.example", "topic": ntfy_topic},
        "desktop": {"toast": toast, "sound": sound},
    }
    path = tmp_path / "settings.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return load_settings(path)


def test_the_ntfy_placeholder_topic_is_never_used(tmp_path):
    channels, notes = build_channels(settings_with(tmp_path, "CHANGE-ME-to-a-long-random-string", toast=False, sound=False))

    assert channels == []
    assert any("placeholder" in note for note in notes)


def test_channels_follow_the_settings(tmp_path):
    channels, notes = build_channels(settings_with(tmp_path, "my-private-topic-1234"))
    assert sorted(c.name for c in channels) == ["desktop", "ntfy"] and notes == []

    channels, _ = build_channels(settings_with(tmp_path, "my-private-topic-1234", enabled=False, toast=False, sound=False))
    assert channels == []


@pytest.fixture
def ntfy_server():
    received = []
    state = {"status": 200}

    class Handler(BaseHTTPRequestHandler):
        def _record(self):
            length = int(self.headers.get("Content-Length") or 0)
            received.append(
                {
                    "method": self.command,
                    "path": self.path,
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                    "body": self.rfile.read(length),
                }
            )
            self.send_response(state["status"])
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

        do_POST = do_PUT = _record

        def log_message(self, *args):
            pass

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}", received, state
    httpd.shutdown()
    httpd.server_close()


def test_ntfy_posts_the_message_with_title_link_and_priority(ntfy_server):
    server, received, _ = ntfy_server

    NtfyChannel(server, "my-topic").send(
        Notification("Job needs you: CAPTCHA", "Frontend Engineer at Acme", url=URL, priority="high")
    )

    request = received[0]
    assert (request["method"], request["path"]) == ("POST", "/my-topic")
    assert request["body"] == b"Frontend Engineer at Acme"
    assert request["headers"]["title"] == "Job needs you: CAPTCHA"
    assert request["headers"]["click"] == URL and request["headers"]["priority"] == "high"
    assert request["headers"]["user-agent"].startswith("jobagent/")


def test_ntfy_encodes_non_ascii_text_so_headers_stay_valid(ntfy_server):
    server, received, _ = ntfy_server

    NtfyChannel(server, "my-topic").send(Notification("Équipe bloquée", "नमस्ते"))

    assert received[0]["headers"]["title"].startswith("=?utf-8?")
    assert received[0]["body"].decode("utf-8") == "नमस्ते"


def test_ntfy_uploads_the_screenshot_as_an_attachment(ntfy_server, tmp_path):
    server, received, _ = ntfy_server
    shot = tmp_path / "screenshot.png"
    shot.write_bytes(b"PNGDATA")

    NtfyChannel(server, "my-topic").send(Notification("T", "Look at this", attachment=str(shot)))

    request = received[0]
    assert request["method"] == "PUT" and request["body"] == b"PNGDATA"
    assert request["headers"]["filename"] == "screenshot.png" and request["headers"]["message"] == "Look at this"


def test_a_missing_screenshot_file_falls_back_to_a_plain_message(ntfy_server, tmp_path):
    server, received, _ = ntfy_server

    NtfyChannel(server, "my-topic").send(Notification("T", "No picture", attachment=str(tmp_path / "gone.png")))

    assert received[0]["method"] == "POST" and received[0]["body"] == b"No picture"


def test_ntfy_reports_an_http_error_and_an_unreachable_server(ntfy_server):
    server, _, state = ntfy_server
    state["status"] = 500
    with pytest.raises(NotifyError, match="500"):
        NtfyChannel(server, "my-topic").send(Notification("T", "m"))

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    with pytest.raises(NotifyError, match="cannot reach"):
        NtfyChannel(f"http://127.0.0.1:{port}", "my-topic").send(Notification("T", "m"))


def run_recorder(returncode=0, stderr=""):
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=returncode, stderr=stderr)

    return run, calls


def test_the_desktop_toast_passes_the_text_through_the_environment_not_the_script():
    run, calls = run_recorder()
    beeps = []

    DesktopChannel(toast=True, sound=True, run=run, beep=lambda: beeps.append(1), platform="win32").send(
        Notification("Job needs you: CAPTCHA", "Acme'; Remove-Item C:\\ -Recurse #")
    )

    args, kwargs = calls[0]
    script = args[-1]
    assert args[0].lower().endswith("powershell.exe") or args[0].lower() == "powershell"
    assert "Remove-Item" not in script and "CAPTCHA" not in script
    assert kwargs["env"]["JOBAGENT_TITLE"] == "Job needs you: CAPTCHA"
    assert "Remove-Item" in kwargs["env"]["JOBAGENT_MESSAGE"]
    assert beeps == [1]


def test_toast_and_sound_can_be_switched_off_independently():
    run, calls = run_recorder()
    beeps = []

    DesktopChannel(toast=False, sound=True, run=run, beep=lambda: beeps.append(1), platform="win32").send(Notification("T", "m"))
    assert calls == [] and beeps == [1]

    DesktopChannel(toast=True, sound=False, run=run, beep=lambda: beeps.append(1), platform="win32").send(Notification("T", "m"))
    assert len(calls) == 1 and beeps == [1]


def test_the_desktop_channel_only_works_on_windows():
    with pytest.raises(NotifyError, match="Windows"):
        DesktopChannel(toast=True, sound=True, platform="linux").send(Notification("T", "m"))


def test_a_failing_toast_reports_what_powershell_said():
    run, _ = run_recorder(returncode=1, stderr="Show failed: no app id")

    with pytest.raises(NotifyError, match="no app id"):
        DesktopChannel(toast=True, sound=False, run=run, platform="win32").send(Notification("T", "m"))
