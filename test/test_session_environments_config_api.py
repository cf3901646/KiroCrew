"""The dashboard config API for the session_environments section: the masked
GET and the PATCH that validates with the loader's own normalizers."""

from __future__ import annotations

import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from dashboard_owner_helpers import as_owner
from test_session_environments_config import (
    _BASE_CONFIG,
    _DEFAULTS,
    _profile,
    _stored,
    _write_section,
)

from kiro_crew.dashboard.handlers import api_kirocrew_config, api_kirocrew_config_patch


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(_BASE_CONFIG), encoding="utf-8")
    monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: path)
    return path


def _app() -> web.Application:
    app = web.Application()
    app.router.add_patch("/api/config/kirocrew", api_kirocrew_config_patch)
    app.router.add_get("/api/config/kirocrew", api_kirocrew_config)
    return as_owner(app)


async def _patch(client, path, value):
    return await client.patch("/api/config/kirocrew", json={"path": path, "value": value})


async def _section(client) -> dict:
    return (await (await client.get("/api/config/kirocrew")).json())["session_environments"]


def _api() -> TestClient:
    return TestClient(TestServer(_app()))


class TestApi:
    pytestmark = pytest.mark.asyncio

    async def test_get_exposes_safe_defaults(self, config_file):
        async with _api() as client:
            resp = await client.get("/api/config/kirocrew")
            assert resp.status == 200
            assert (await resp.json())["session_environments"] == {
                "coder_url": "",
                "profiles": [],
                **_DEFAULTS,
            }

    async def test_whole_section_saves_and_preserves_unrelated_config(self, config_file):
        section = {
            "coder_url": " https://c.example.com ",
            "profiles": [_profile("a"), _profile("b", name="  B  ")],
        }
        async with _api() as client:
            resp = await _patch(client, "session_environments", section)
            assert resp.status == 200, await resp.text()
            body = await resp.json()
        expected = {
            "coder_url": "https://c.example.com",
            "profiles": [_profile("a"), _profile("b", name="B")],
        }
        # The read shows the retention default; the file holds only what was sent.
        assert body["session_environments"] == {**expected, **_DEFAULTS}
        stored = _stored(config_file)
        assert stored["session_environments"] == expected
        assert stored["session"]["timeout_secs"] == 1234
        assert stored["an_edition_section"] == {"keep": "me"}
        assert stored["default_agent"] == "kirocrew"

    async def test_leaf_paths_update_one_half(self, config_file):
        async with _api() as client:
            assert (
                await _patch(client, "session_environments.profiles", [_profile()])
            ).status == 200
            resp = await _patch(client, "session_environments.coder_url", "https://c.example.com")
            assert resp.status == 200
            got = await _section(client)
        assert got == {"coder_url": "https://c.example.com", "profiles": [_profile()], **_DEFAULTS}

    @pytest.mark.parametrize(
        ("path", "value"),
        [
            ("session_environments", {"coder_url": "https://c.example.com"}),
            ("session_environments", {"coder_url": "", "profiles": [], "token": "x"}),
            ("session_environments", "https://c.example.com"),
            ("session_environments.coder_url", "https://c.example.com?token=x"),
            ("session_environments.coder_url", "file:///etc/passwd"),
            ("session_environments.profiles", [_profile("a"), _profile("a")]),
            ("session_environments.profiles", [_profile(workdir="repo")]),
            ("session_environments.profiles", [{**_profile(), "password": "x"}]),
            ("session_environments.profiles", [_profile(f"p{i}") for i in range(21)]),
            ("session_environments.enabled", True),
        ],
    )
    async def test_invalid_values_are_refused_and_nothing_is_written(
        self, config_file, path, value
    ):
        before = config_file.read_bytes()
        async with _api() as client:
            resp = await _patch(client, path, value)
            assert resp.status == 400
            assert (await resp.json())["error"]
        assert config_file.read_bytes() == before

    async def test_get_drops_unmodelled_keys_but_save_keeps_them(self, config_file):
        """A key a newer build wrote survives the write but is not shown (the schema
        cannot vouch that it is not secret)."""
        _write_section(config_file, {"coder_url": "", "profiles": [], "future": "x"})
        async with _api() as client:
            resp = await _patch(client, "session_environments.coder_url", "https://c.example.com")
            assert resp.status == 200
            got = await _section(client)
        assert got == {"coder_url": "https://c.example.com", "profiles": [], **_DEFAULTS}
        assert _stored(config_file)["session_environments"]["future"] == "x"

    async def test_an_older_client_that_omits_an_optional_key_keeps_the_saved_value(
        self, config_file
    ):
        async with _api() as client:
            assert (
                await _patch(client, "session_environments.stop_on_shutdown", False)
            ).status == 200
            # A dashboard from before the setting saves the section without it.
            resp = await _patch(
                client,
                "session_environments",
                {"coder_url": "https://c.example.com", "profiles": []},
            )
            assert resp.status == 200, await resp.text()
            got = await _section(client)
        assert got["stop_on_shutdown"] is False

    @pytest.mark.parametrize("field", ["", ".archived_retention_days"])
    async def test_retention_is_not_a_setting(self, config_file, field):
        # Fixed at ARCHIVED_RETENTION_DAYS: refused as a field and as a section key.
        section = {"coder_url": "https://c.example.com", "profiles": []}
        value = 2 if field else {**section, "archived_retention_days": 2}
        before = config_file.read_bytes()
        async with _api() as client:
            resp = await _patch(client, "session_environments" + field, value)
            assert resp.status == 400
        assert config_file.read_bytes() == before
