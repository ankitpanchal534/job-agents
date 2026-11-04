from __future__ import annotations

import os
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.header import Header
from pathlib import Path
from typing import Callable, Optional, Protocol
from urllib.parse import quote

from jobagent.config import Settings
from jobagent.llm import USER_AGENT
from jobagent.storage import Block, Storage

REASON_LABELS = {
    "captcha": "CAPTCHA",
    "login_expired": "Login expired",
    "unknown_question": "Unknown question",
    "unsupported_portal": "Unsupported portal",
    "submit_unconfirmed": "Submit not confirmed",
    "other": "Needs you",
}

TOAST_SCRIPT = """
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
$xml = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$nodes = $xml.GetElementsByTagName('text')
$nodes.Item(0).AppendChild($xml.CreateTextNode($env:JOBAGENT_TITLE)) | Out-Null
$nodes.Item(1).AppendChild($xml.CreateTextNode($env:JOBAGENT_MESSAGE)) | Out-Null
$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
$appId = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId).Show($toast)
"""

PLACEHOLDER_TOPIC_PREFIX = "CHANGE-ME"


class NotifyError(Exception):
    pass


@dataclass(frozen=True)
class Notification:
    title: str
    message: str
    url: Optional[str] = None
    attachment: Optional[str] = None
    priority: str = "default"


class Channel(Protocol):
    name: str

    def send(self, notification: Notification) -> None: ...


def _header(value: str) -> str:
    value = " ".join(value.split())
    try:
        value.encode("ascii")
        return value
    except UnicodeEncodeError:
        return Header(value, "utf-8", maxlinelen=1_000_000).encode()


class NtfyChannel:
    name = "ntfy"

    def __init__(self, server: str, topic: str, timeout: float = 20):
        self._server = server
        self._topic = topic
        self._timeout = timeout

    def send(self, notification: Notification) -> None:
        url = f"{self._server.rstrip('/')}/{quote(self._topic, safe='')}"
        headers = {"User-Agent": USER_AGENT, "Title": _header(notification.title), "Priority": notification.priority}
        if notification.url:
            headers["Click"] = quote(notification.url, safe="/:?&=#%@+,;~")

        screenshot = Path(notification.attachment) if notification.attachment else None
        if screenshot is not None and screenshot.is_file():
            method, body = "PUT", screenshot.read_bytes()
            headers.update(
                {
                    "Filename": _header(screenshot.name),
                    "Message": _header(notification.message),
                    "Content-Type": "application/octet-stream",
                }
            )
        else:
            method, body = "POST", notification.message.encode("utf-8")
            headers["Content-Type"] = "text/plain; charset=utf-8"

        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            urllib.request.urlopen(request, timeout=self._timeout).close()
        except urllib.error.HTTPError as error:
            raise NotifyError(f"ntfy returned HTTP {error.code}") from error
        except OSError as error:
            raise NotifyError(f"cannot reach {self._server}: {getattr(error, 'reason', error)}") from error


def _beep() -> None:
    import winsound

    winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)


class DesktopChannel:
    name = "desktop"

    def __init__(
        self,
        toast: bool = True,
        sound: bool = True,
        run: Callable = subprocess.run,
        beep: Optional[Callable[[], None]] = None,
        platform: str = sys.platform,
    ):
        self._toast = toast
        self._sound = sound
        self._run = run
        self._beep = beep or _beep
        self._platform = platform

    def send(self, notification: Notification) -> None:
        if self._platform != "win32":
            raise NotifyError("desktop notifications only work on Windows")
        if self._sound:
            self._beep()
        if self._toast:
            result = self._run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", TOAST_SCRIPT],
                env={**os.environ, "JOBAGENT_TITLE": notification.title, "JOBAGENT_MESSAGE": notification.message},
                capture_output=True,
                text=True,
                timeout=20,
            )
            if result.returncode != 0:
                raise NotifyError(f"toast failed: {result.stderr.strip()[:200]}")


def build_channels(settings: Settings) -> tuple[list[Channel], list[str]]:
    config = settings.notifications
    channels: list[Channel] = []
    notes: list[str] = []
    if config.ntfy.enabled:
        if config.ntfy.topic.startswith(PLACEHOLDER_TOPIC_PREFIX):
            notes.append(
                "ntfy is enabled but its topic is still the placeholder: not sending "
                "(ntfy.sh topics are public, so set a long random one in settings.yaml)."
            )
        else:
            channels.append(NtfyChannel(config.ntfy.server, config.ntfy.topic))
    if config.desktop.toast or config.desktop.sound:
        channels.append(DesktopChannel(toast=config.desktop.toast, sound=config.desktop.sound))
    return channels, notes


@dataclass(frozen=True)
class NotifyReport:
    sent: list[str]
    failed: dict[str, str]
    suppressed: bool


class Notifier:
    def __init__(
        self,
        storage: Storage,
        channels: list[Channel],
        cooldown_minutes: float = 15,
        clock: Optional[Callable[[], datetime]] = None,
    ):
        self._storage = storage
        self._channels = channels
        self._cooldown = timedelta(minutes=cooldown_minutes)
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def notify_block(self, block: Block) -> NotifyReport:
        last = self._storage.last_alert(block.reason, block.host)
        if last and self._clock() - datetime.fromisoformat(last) < self._cooldown:
            return NotifyReport(sent=[], failed={}, suppressed=True)

        held = self._storage.unalerted_count(block.reason, block.host, since=last, exclude_id=block.id)
        job = self._storage.get_job(block.job_id)
        message = f"{job.title or 'Untitled role'} at {job.company or 'unknown company'}"
        if block.detail:
            message += f"\n{block.detail}"
        if held:
            message += f"\n(+{held} similar since the last alert)"
        notification = Notification(
            title=f"Job needs you: {REASON_LABELS[block.reason]}",
            message=message,
            url=job.url,
            attachment=block.screenshot_path,
            priority="high",
        )

        sent: list[str] = []
        failed: dict[str, str] = {}
        for channel in self._channels:
            try:
                channel.send(notification)
                sent.append(channel.name)
            except NotifyError as error:
                failed[channel.name] = str(error)
        if sent:
            self._storage.mark_block_notified(block.id)
        return NotifyReport(sent=sent, failed=failed, suppressed=False)

    def notify_applied(
        self,
        job_id: int,
        screenshot_path: Optional[str] = None,
        custom_message: Optional[str] = None,
    ) -> NotifyReport:
        job = self._storage.get_job(job_id)
        if not job:
            return NotifyReport(sent=[], failed={"error": f"Job #{job_id} not found"}, suppressed=False)

        title = "Job Applied"
        job_info = f"{job.title or 'Application'} at {job.company or 'Company'}"
        message = custom_message or f"Job Applied: {job_info}\nLink: {job.url}"

        notification = Notification(
            title=title,
            message=message,
            url=job.url,
            attachment=screenshot_path,
            priority="high",
        )

        sent: list[str] = []
        failed: dict[str, str] = {}
        for channel in self._channels:
            try:
                channel.send(notification)
                sent.append(channel.name)
            except NotifyError as error:
                failed[channel.name] = str(error)
        return NotifyReport(sent=sent, failed=failed, suppressed=False)

