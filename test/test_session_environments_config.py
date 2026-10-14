"""Persisted Session Environments settings: validator, loader and dashboard API.

One validator serves both: a hand-edited file keeps its valid profiles while
``PATCH`` refuses the same value and writes nothing.
"""

from __future__ import annotations

import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from dashboard_owner_helpers import as_owner

from kiro_crew.config import session_environments as se
from kiro_crew.config.loader import _KNOWN_CONFIG_SECTIONS, KiroCrewConfig
from kiro_crew.dashboard.handlers import api_kirocrew_config, api_kirocrew_config_patch

_BASE_CONFIG = {
    "agents": {"kirocrew": {"kiro_agent": "kirocrew"}},
    "default_agent": "kirocrew",
    "session": {"timeout_secs": 1234},
    "an_edition_section": {"keep": "me"},
}
#: What a read adds to a section that omits them.
_DEFAULTS = {"kiro_cloud_profiles": [], "default_profile": "", "stop_on_shutdown": True}


def _profile(pid: str = "dev", **overrides: object) -> dict:
    base = {
        "id": pid,
        "name": "Dev box",
        "template": "python-dev",
        "workdir": "/workspace/repo",
        "compute_survives_stop": True,
    }
    base.update(overrides)
    return base


def _app() -> web.Application:
    app = web.Application()
    app.router.add_patch("/api/config/kirocrew", api_kirocrew_config_patch)
    app.router.add_get("/api/config/kirocrew", api_kirocrew_config)
    return as_owner(app)


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(_BASE_CONFIG), encoding="utf-8")
    monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: path)
    return path


def _stored(path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


async def _patch(client, path, value):
    return await client.patch("/api/config/kirocrew", json={"path": path, "value": value})


async def _section(client) -> dict:
    return (await (await client.get("/api/config/kirocrew")).json())["session_environments"]


def _api() -> TestClient:
    return TestClient(TestServer(_app()))


def _write_section(path, section) -> None:
    path.write_text(json.dumps({**_BASE_CONFIG, "session_environments": section}), encoding="utf-8")


class TestCoderUrl:
    @pytest.mark.parametrize(
        "url",
        ["", "https://coder.example.com", "http://10.0.0.5:3000", "https://c.example.com/base/"],
    )
    def test_accepts(self, url):
        assert se.validate_coder_url(url) == url

    def test_strips_whitespace(self):
        assert se.validate_coder_url("  https://c.example.com  ") == "https://c.example.com"

    @pytest.mark.parametrize(
        "url",
        [
            "ftp://c.example.com",
            "c.example.com",
            "https://",
            "https://user@c.example.com",
            "https://user:pw@c.example.com",
            "https://c.example.com?x=1",
            "https://c.example.com?",
            "https://c.example.com#frag",
            "https://c.example.com:99999",
            "https://c.exa mple.com",
            "https://c.exa\tmple.com",
            "javascript:alert(1)",
            123,
            None,
        ],
    )
    def test_rejects(self, url):
        with pytest.raises(se.SessionEnvironmentsError):
            se.validate_coder_url(url)


class TestProfiles:
    def test_valid_list(self):
        profiles = [_profile("a"), _profile("B-2_x")]
        assert se.validate_profiles(profiles) == profiles

    def test_id_max_length(self):
        assert se.validate_profile(_profile("a" * 64))["id"] == "a" * 64
        with pytest.raises(se.SessionEnvironmentsError):
            se.validate_profile(_profile("a" * 65))

    @pytest.mark.parametrize("pid", ["", "-dev", "_dev", "dev box", "dév", "dev.1", "dev\n", 5])
    def test_bad_id(self, pid):
        with pytest.raises(se.SessionEnvironmentsError, match=r"\.id"):
            se.validate_profile(_profile(pid))

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("name", ""),
            ("name", "   "),
            ("name", "x" * 129),
            ("name", 7),
            ("template", ""),
            ("template", "t" * 256),
            ("template", "a\x00b"),
            ("workdir", ""),
            ("workdir", "relative/path"),
            ("workdir", "C:\\repo"),
            ("workdir", "/" + "p" * 1024),
        ],
    )
    def test_bad_field(self, field, value):
        with pytest.raises(se.SessionEnvironmentsError, match=field):
            se.validate_profile(_profile(**{field: value}))

    def test_bounds_inclusive(self):
        p = _profile(name="n" * 128, template="t" * 255, workdir="/" + "p" * 1023)
        assert se.validate_profile(p) == p

    def test_unknown_key_refused_so_no_credential_can_ride_along(self):
        with pytest.raises(se.SessionEnvironmentsError, match="unknown"):
            se.validate_profile({**_profile(), "token": "secret"})

    def test_missing_key(self):
        p = _profile()
        del p["workdir"]
        with pytest.raises(se.SessionEnvironmentsError, match="missing"):
            se.validate_profile(p)

    def test_duplicate_id(self):
        with pytest.raises(se.SessionEnvironmentsError, match="duplicate"):
            se.validate_profiles([_profile("a"), _profile("a")])

    def test_cap(self):
        ok = [_profile(f"p{i}") for i in range(se.MAX_PROFILES)]
        assert len(se.validate_profiles(ok)) == se.MAX_PROFILES
        with pytest.raises(se.SessionEnvironmentsError, match="at most"):
            se.validate_profiles(ok + [_profile("extra")])

    @pytest.mark.parametrize("value", [None, {}, "a", ({"id": "a"},)])
    def test_not_a_list(self, value):
        with pytest.raises(se.SessionEnvironmentsError):
            se.validate_profiles(value)


class TestSection:
    def test_both_keys_required_and_no_others(self):
        good = {"coder_url": "", "profiles": []}
        assert se.validate_session_environments(good) == good
        with pytest.raises(se.SessionEnvironmentsError, match="missing"):
            se.validate_session_environments({"coder_url": ""})
        with pytest.raises(se.SessionEnvironmentsError, match="unknown"):
            se.validate_session_environments({**good, "enabled": True})
        with pytest.raises(se.SessionEnvironmentsError):
            se.validate_session_environments([])


class TestLoader:
    def test_absent_section_defaults(self, config_file):
        cfg = KiroCrewConfig.load()
        assert cfg.session_environments.coder_url == ""
        assert cfg.session_environments.profiles == []
        assert cfg.to_dict()["session_environments"] == {
            "coder_url": "",
            "profiles": [],
            **_DEFAULTS,
        }

    def test_valid_section_round_trips(self, config_file):
        section = {
            "coder_url": "https://c.example.com",
            "profiles": [_profile("a")],
            "kiro_cloud_profiles": [],
            "default_profile": "",
            "stop_on_shutdown": True,
        }
        _write_section(config_file, section)
        cfg = KiroCrewConfig.load()
        assert cfg.to_dict()["session_environments"] == section
        cfg.save()
        stored = _stored(config_file)
        assert stored["session_environments"] == section
        assert stored["an_edition_section"] == {"keep": "me"}
        assert KiroCrewConfig.load().to_dict()["session_environments"] == section

    def test_lenient_load_keeps_valid_parts(self, config_file):
        section = {
            "coder_url": "https://user:pw@c.example.com",
            "profiles": [
                _profile("good"),
                _profile("bad", workdir="relative"),
                _profile("good"),
                "not-an-object",
                _profile("also-good"),
            ],
        }
        _write_section(config_file, section)
        env = KiroCrewConfig.load().session_environments
        assert env.coder_url == ""
        assert [p.id for p in env.profiles] == ["good", "also-good"]

    def test_over_cap_truncated_on_load(self):
        coder_url, profiles = se.load_session_environments(
            {"profiles": [_profile(f"p{i}") for i in range(se.MAX_PROFILES + 5)]}
        )
        assert coder_url == ""
        assert len(profiles) == se.MAX_PROFILES

    @pytest.mark.parametrize("raw", [None, "x", [], {"profiles": "x"}])
    def test_malformed_section_defaults(self, raw):
        assert se.load_session_environments(raw) == ("", [])

    def test_is_a_known_section(self):
        assert "session_environments" in _KNOWN_CONFIG_SECTIONS


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

    @pytest.mark.asyncio
    async def test_a_kiro_cloud_profile_round_trips_and_survives_an_older_client(self, config_file):
        cloud = {"id": "cloud", "name": "Kiro Cloud", "repositories": ["acme/web"]}
        async with _api() as client:
            resp = await _patch(client, "session_environments.kiro_cloud_profiles", [cloud])
            assert resp.status == 200, await resp.text()
            # A dashboard that predates Kiro Cloud saves the section without it.
            resp = await _patch(
                client,
                "session_environments",
                {"coder_url": "https://c.example.com", "profiles": []},
            )
            assert resp.status == 200, await resp.text()
            got = await _section(client)
            # The id decides the provider, so a Coder profile may not reuse it.
            clash = await _patch(
                client,
                "session_environments",
                {
                    "coder_url": "https://c.example.com",
                    "profiles": [_profile("cloud")],
                    "kiro_cloud_profiles": [cloud],
                },
            )
            assert clash.status == 400
        assert got["kiro_cloud_profiles"] == [cloud]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "path,value",
        [
            # A leaf write of either provider's list, checked against the other on disk.
            ("session_environments.profiles", [_profile("cloud")]),
            (
                "session_environments.kiro_cloud_profiles",
                [{"id": "dev", "name": "Dev", "repositories": []}],
            ),
            # A whole-section save that omits the cloud list, which the writer preserves.
            (
                "session_environments",
                {"coder_url": "https://c.example.com", "profiles": [_profile("cloud")]},
            ),
        ],
    )
    async def test_a_write_that_would_share_an_id_across_providers_is_refused(
        self, config_file, path, value
    ):
        async with _api() as client:
            seed = await _patch(
                client,
                "session_environments",
                {
                    "coder_url": "https://c.example.com",
                    "profiles": [_profile("dev")],
                    "kiro_cloud_profiles": [
                        {"id": "cloud", "name": "Kiro Cloud", "repositories": []}
                    ],
                },
            )
            assert seed.status == 200, await seed.text()
            before = config_file.read_bytes()
            resp = await _patch(client, path, value)
            assert resp.status == 400, await resp.text()
            assert (await resp.json())["code"] == "profile_id_collision"
        assert config_file.read_bytes() == before

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "stored",
        [
            {"profiles": [{"name": "no id"}, "not an object", None, _profile("dev")]},
            {"profiles": None, "kiro_cloud_profiles": None},
            {"profiles": [], "kiro_cloud_profiles": [{"name": "no id"}, 7, None]},
            {"profiles": [_profile("dev", id=3)], "kiro_cloud_profiles": [{"id": 3}]},
        ],
    )
    async def test_a_hand_edited_entry_the_loader_drops_does_not_break_a_write(
        self, config_file, stored
    ):
        """The id check reads each list the way the loader does, so a dropped entry cannot 500."""
        _write_section(config_file, {"coder_url": "https://c.example.com", **stored})
        cloud = [{"id": "cloud", "name": "Kiro Cloud", "repositories": []}]
        async with _api() as client:
            # A leaf that rewrites neither list: the check reads both as stored.
            resp = await _patch(client, "session_environments.stop_on_shutdown", False)
            assert resp.status == 200, await resp.text()
            resp = await _patch(client, "session_environments.kiro_cloud_profiles", cloud)
            assert resp.status == 200, await resp.text()
            resp = await _patch(client, "session_environments.default_profile", "cloud")
            assert resp.status == 200, await resp.text()
        assert _stored(config_file)["session_environments"]["kiro_cloud_profiles"] == cloud
