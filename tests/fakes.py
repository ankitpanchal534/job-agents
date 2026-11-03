from datetime import datetime, timedelta, timezone

from jobagent.notify import NotifyError


class FakeLLM:
    def __init__(self, model_list=(), **replies):
        self.replies = replies
        self.model_list = list(model_list)
        self.listed_provider = None
        self.calls = []

    def list_models(self, provider=None):
        self.listed_provider = provider
        return list(self.model_list)

    def complete(self, task, system, user):
        self.calls.append((task, system, user))
        reply = self.replies.get(task, "INSUFFICIENT")
        if isinstance(reply, Exception):
            raise reply
        return reply

    @property
    def tasks(self):
        return [task for task, _, _ in self.calls]


class Clock:
    def __init__(self):
        self.now = datetime(2026, 11, 3, 10, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, minutes=1):
        self.now += timedelta(minutes=minutes)


class FakeChannel:
    def __init__(self, name="fake", fail=False):
        self.name = name
        self.fail = fail
        self.sent = []

    def send(self, notification):
        if self.fail:
            raise NotifyError(f"{self.name} is down")
        self.sent.append(notification)
