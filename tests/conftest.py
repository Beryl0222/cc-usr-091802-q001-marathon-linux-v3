"""pytest 公共夹具：内存赛务应用与极简 HTTP 调用助手。"""

import json

import pytest

from app.api import ApiApp


class Client:
    def __init__(self, app):
        self.app = app

    def call(self, method, path, body=None, role=None):
        headers = {}
        if role:
            headers["x-staff-role"] = role
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else b""
        status, payload = self.app.handle(method, path, headers, raw)
        return status, payload

    def post(self, path, body, role="official"):
        return self.call("POST", path, body, role)

    def get(self, path, role="official"):
        return self.call("GET", path, None, role)


@pytest.fixture
def app():
    return ApiApp()


@pytest.fixture
def client(app):
    return Client(app)


@pytest.fixture
def seeded(app):
    """灌入演练事件后的应用。"""
    from app import scenarios
    scenarios.load(app)
    with app.store.lock():
        app.state.rebuild(app.store.canonical_events())
    return app


@pytest.fixture
def state(seeded):
    """演练事件重放后的赛务状态投影。"""
    return seeded.state


@pytest.fixture
def seeded_client(seeded):
    return Client(seeded)
