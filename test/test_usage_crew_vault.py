"""Credit usage from Crew's own sign-in vault, with no kiro-cli on the host.

Covers the vault branch of ``_fetch_usage_bg`` (``_refresh_from_vault``), the
harness-scoped ownership verdict (``_vault_owns_usage_sync``), the readiness-gate
bypass on ``/api/sessions/usage`` and its refresh route, and the single-credential
API client ``fetch_usage_limits_with_credential``.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import kiro_crew.dashboard.handlers.kiro_usage_api as api
import kiro_crew.dashboard.handlers.sessions as sessions_mod

_FP_A = "fp-a"
_FP_B = "fp-b"
_USAGE = {
    "credits_plan": 1000.0,
    "credits_used": 42.0,
    "plan": "KIRO PRO",
    "_profile_arn": "arn:x",
}


def _cred(profile_arn: str | None = "arn:aws:codewhisperer:us-east-1:1:profile/p"):
    return SimpleNamespace(
        access_token="FAKE-access",
        profile_arn=profile_arn,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )


@pytest.fixture(autouse=True)
def _reset():
    def reset():
        sessions_mod._usage_cache = {}
        sessions_mod._usage_cache_ts = 0.0
        sessions_mod._usage_fetching = False
        sessions_mod._usage_cache_vault_fp = ""

    reset()
    yield
    reset()


def _vault(*, fps=(_FP_A, _FP_A), current=None, result=None):
    """Patch the vault seams: fingerprint sequence, provider, API client."""
    fp_iter = iter(fps)
    provider = MagicMock()
    provider.current = current or AsyncMock(return_value=_cred())
    if result is None:
        result = api.UsageResult(dict(_USAGE), api.AUTH_OK)
    return [
        patch("kiro_crew.auth.bridge.vault_identity_fingerprint", lambda: next(fp_iter)),
        patch("kiro_crew.auth.bridge.default_token_store", MagicMock()),
        patch("kiro_crew.auth.provider.KasAuthProvider", MagicMock(return_value=provider)),
        patch.object(
            sessions_mod.kiro_usage_api,
            "fetch_usage_limits_with_credential",
            MagicMock(return_value=result),
        ),
    ]


async def _run(patches, *, vault_owned=True):
    resolve = AsyncMock(return_value=None)
    for p in patches:
        p.start()
    try:
        with patch.object(sessions_mod, "_resolve_kiro_bin_for_spawn", resolve):
            await sessions_mod._fetch_usage_bg(vault_owned=vault_owned)
    finally:
        for p in reversed(patches):
            p.stop()
    return resolve


class TestRefreshFromVault:
    @pytest.mark.asyncio
    async def test_publishes_reading_without_kiro_cli(self):
        resolve = await _run(_vault())
        assert sessions_mod._usage_cache["credits_plan"] == 1000.0
        assert "_profile_arn" not in sessions_mod._usage_cache
        assert sessions_mod._usage_cache_vault_fp == _FP_A
        resolve.assert_not_called()

    @pytest.mark.asyncio
    async def test_not_vault_owned_takes_kiro_cli_path(self):
        resolve = await _run(_vault(), vault_owned=False)
        resolve.assert_called_once()
        assert sessions_mod._usage_cache == {"available": False}

    @pytest.mark.asyncio
    async def test_identity_vanished_after_verdict_never_falls_back(self):
        # The handler skipped the kiro-cli readiness gate on a vault verdict; a
        # logout before the refresh reads the vault must not reach kiro-cli.
        resolve = await _run(_vault(fps=("",)))
        assert sessions_mod._usage_cache == {"available": False, "reason": "signin_required"}
        resolve.assert_not_called()

    @pytest.mark.asyncio
    async def test_signed_out_during_token_read_never_falls_back(self):
        from kiro_crew.auth.provider import NotAuthenticated

        current = AsyncMock(side_effect=NotAuthenticated("gone"))
        resolve = await _run(_vault(current=current))
        assert sessions_mod._usage_cache == {"available": False, "reason": "signin_required"}
        resolve.assert_not_called()

    @pytest.mark.asyncio
    async def test_identity_change_discards_reading(self):
        await _run(_vault(fps=(_FP_A, _FP_B)))
        assert sessions_mod._usage_cache == {"available": False}

    @pytest.mark.asyncio
    async def test_auth_rejected_reports_signin(self):
        resolve = await _run(_vault(result=api.UsageResult(None, api.AUTH_REJECTED)))
        assert sessions_mod._usage_cache == {"available": False, "reason": "signin_required"}
        resolve.assert_not_called()

    @pytest.mark.asyncio
    async def test_transient_refresh_failure_does_not_ask_for_signin(self):
        # An issuer 5xx or a network error after wake leaves the grant valid.
        current = AsyncMock(side_effect=RuntimeError("issuer unreachable"))
        resolve = await _run(_vault(current=current))
        assert sessions_mod._usage_cache == {"available": False}
        resolve.assert_not_called()

    @pytest.mark.asyncio
    async def test_rejected_refresh_reports_signin(self):
        from kiro_crew.auth.refresh import RefreshRejected

        current = AsyncMock(side_effect=RefreshRejected("social refresh failed: HTTP 400"))
        resolve = await _run(_vault(current=current))
        assert sessions_mod._usage_cache == {"available": False, "reason": "signin_required"}
        resolve.assert_not_called()

    @pytest.mark.asyncio
    async def test_failure_keeps_same_identity_reading_stale(self):
        sessions_mod._usage_cache = {"credits_plan": 1000.0, "credits_used": 1.0}
        sessions_mod._usage_cache_vault_fp = _FP_A
        await _run(_vault(result=api.UsageResult(None, api.AUTH_OTHER)))
        assert sessions_mod._usage_cache["stale"] is True
        assert sessions_mod._usage_cache["credits_used"] == 1.0

    @pytest.mark.asyncio
    async def test_failure_never_keeps_kiro_cli_reading(self):
        # A reading published by the kiro-cli path carries no vault fingerprint.
        sessions_mod._usage_cache = {"credits_plan": 1000.0, "credits_used": 1.0}
        await _run(_vault(result=api.UsageResult(None, api.AUTH_OTHER)))
        assert sessions_mod._usage_cache == {"available": False}


def _config(backend: str):
    return SimpleNamespace(agent=SimpleNamespace(acp_backend=backend))


class TestVaultOwnership:
    """The vault owns usage only for the harness that draws its credential from it."""

    @staticmethod
    def _store(token):
        store = MagicMock()
        store.resolve.return_value = token
        return patch("kiro_crew.auth.bridge.default_token_store", return_value=store)

    @staticmethod
    def _token(usable: bool):
        tok = MagicMock()
        tok.is_usable.return_value = usable
        return tok

    @staticmethod
    def _holds(value: bool):
        return patch("kiro_crew.auth.bridge.vault_holds_identity", MagicMock(return_value=value))

    def test_kas_with_vault_identity_owns(self):
        with (
            patch("kiro_crew.config.loader.KiroCrewConfig.load", return_value=_config("kas")),
            self._holds(True) as holds,
        ):
            assert sessions_mod._vault_owns_usage_sync() is True
        # The spawn's own predicate, asked without its spawn-time WARNING.
        holds.assert_called_once_with(warn=False)

    def test_kiro_cli_backend_never_owns_even_with_vault_identity(self):
        # A leftover Crew sign-in must not relabel a kiro-cli user's usage.
        with (
            patch("kiro_crew.config.loader.KiroCrewConfig.load", return_value=_config("kiro")),
            self._holds(True) as holds,
        ):
            assert sessions_mod._vault_owns_usage_sync() is False
        holds.assert_not_called()

    def test_kas_without_vault_identity_does_not_own(self):
        with (
            patch("kiro_crew.config.loader.KiroCrewConfig.load", return_value=_config("kas")),
            self._holds(False) as holds,
        ):
            assert sessions_mod._vault_owns_usage_sync() is False
        holds.assert_called_once_with(warn=False)

    def test_unusable_vault_identity_does_not_own_and_does_not_warn(self, caplog):
        # This probe runs on every poll; the spawn-time WARNING must not repeat
        # here. Drives the real predicate over a store holding an unusable entry.
        with (
            patch("kiro_crew.config.loader.KiroCrewConfig.load", return_value=_config("kas")),
            self._store(self._token(False)),
            caplog.at_level("DEBUG", logger="kiro_crew.auth.bridge"),
        ):
            assert sessions_mod._vault_owns_usage_sync() is False
        assert not [r for r in caplog.records if r.levelno >= 30]
        # The predicate did reach the unusable entry: its diagnostic is at DEBUG.
        assert any(
            r.name == "kiro_crew.auth.bridge" and r.levelname == "DEBUG" for r in caplog.records
        )

    def test_unreadable_config_keeps_gated_path(self):
        with patch("kiro_crew.config.loader.KiroCrewConfig.load", side_effect=OSError("nope")):
            assert sessions_mod._vault_owns_usage_sync() is False


def _request(method="GET", path="/api/sessions/usage"):
    from aiohttp import web
    from aiohttp.test_utils import make_mocked_request

    app = web.Application()
    app["state"] = SimpleNamespace(_background_tasks=set())
    return make_mocked_request(method, path, app=app)


class TestUsageGateBypass:
    @pytest.mark.asyncio
    async def test_vault_owned_skips_kiro_readiness_gate(self):
        gate = AsyncMock(return_value="BLOCKED")
        sessions_mod._usage_cache_ts = 10**12  # fresh: no background refresh
        sessions_mod._usage_cache = {"credits_plan": 5.0}
        with (
            patch.object(sessions_mod, "_vault_owns_usage", AsyncMock(return_value=True)),
            patch.object(sessions_mod, "reject_if_kiro_unverified", gate),
        ):
            resp = await sessions_mod.api_sessions_usage(_request())
        gate.assert_not_called()
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_fresh_reading_for_another_identity_is_never_served(self):
        # Account A's reading is cached; the Crew sign-in switches to B inside
        # the refresh interval. The GET must not hand A's credits to B.
        sessions_mod._usage_cache_ts = 10**12  # fresh: the timer would not refresh
        sessions_mod._usage_cache = {"credits_plan": 5.0, "credits_used": 1.0}
        sessions_mod._usage_cache_vault_fp = _FP_A
        fetch = AsyncMock(return_value=None)
        with (
            patch.object(sessions_mod, "_vault_owns_usage", AsyncMock(return_value=True)),
            patch("kiro_crew.auth.bridge.vault_identity_fingerprint", return_value=_FP_B),
            patch.object(sessions_mod, "_fetch_usage_bg", fetch),
        ):
            resp = await sessions_mod.api_sessions_usage(_request())
            await asyncio.sleep(0)
        assert json.loads(resp.body) == {"usage": {"available": False}}
        fetch.assert_awaited_once_with(vault_owned=True)

    @pytest.mark.asyncio
    async def test_vault_reading_is_never_served_once_vault_stops_owning(self):
        # The vault published account A's reading; the user then signs out of
        # Crew (or leaves KAS). The kiro-cli path must not serve A's credits.
        sessions_mod._usage_cache_ts = 10**12  # fresh: the timer would not refresh
        sessions_mod._usage_cache = {"credits_plan": 5.0, "credits_used": 1.0}
        sessions_mod._usage_cache_vault_fp = _FP_A
        gate = AsyncMock(return_value=None)
        fetch = AsyncMock(return_value=None)
        with (
            patch.object(sessions_mod, "_vault_owns_usage", AsyncMock(return_value=False)),
            patch.object(sessions_mod, "reject_if_kiro_unverified", gate),
            patch.object(sessions_mod, "_fetch_usage_bg", fetch),
        ):
            resp = await sessions_mod.api_sessions_usage(_request())
            await asyncio.sleep(0)
        gate.assert_awaited_once()
        assert json.loads(resp.body) == {"usage": {"available": False}}
        fetch.assert_awaited_once_with(vault_owned=False)
        # Dropped, so the kiro-cli path can never preserve it as its own.
        assert sessions_mod._usage_cache == {"available": False}
        assert sessions_mod._usage_cache_vault_fp == ""

    @pytest.mark.asyncio
    async def test_kiro_cli_reading_is_served_when_vault_does_not_own(self):
        sessions_mod._usage_cache_ts = 10**12
        sessions_mod._usage_cache = {"credits_plan": 5.0}
        fetch = AsyncMock(return_value=None)
        with (
            patch.object(sessions_mod, "_vault_owns_usage", AsyncMock(return_value=False)),
            patch.object(sessions_mod, "reject_if_kiro_unverified", AsyncMock(return_value=None)),
            patch.object(sessions_mod, "_fetch_usage_bg", fetch),
        ):
            resp = await sessions_mod.api_sessions_usage(_request())
        assert json.loads(resp.body) == {"usage": {"credits_plan": 5.0}}
        fetch.assert_not_called()

    @pytest.mark.asyncio
    async def test_fresh_reading_for_same_identity_is_served(self):
        sessions_mod._usage_cache_ts = 10**12
        sessions_mod._usage_cache = {"credits_plan": 5.0}
        sessions_mod._usage_cache_vault_fp = _FP_A
        fetch = AsyncMock(return_value=None)
        with (
            patch.object(sessions_mod, "_vault_owns_usage", AsyncMock(return_value=True)),
            patch("kiro_crew.auth.bridge.vault_identity_fingerprint", return_value=_FP_A),
            patch.object(sessions_mod, "_fetch_usage_bg", fetch),
        ):
            resp = await sessions_mod.api_sessions_usage(_request())
        assert json.loads(resp.body) == {"usage": {"credits_plan": 5.0}}
        fetch.assert_not_called()

    @pytest.mark.asyncio
    async def test_not_vault_owned_keeps_gate(self):
        blocked = MagicMock()
        gate = AsyncMock(return_value=blocked)
        with (
            patch.object(sessions_mod, "_vault_owns_usage", AsyncMock(return_value=False)),
            patch.object(sessions_mod, "reject_if_kiro_unverified", gate),
        ):
            resp = await sessions_mod.api_sessions_usage(_request())
        assert resp is blocked

    @pytest.mark.asyncio
    async def test_refresh_route_carries_vault_verdict_into_refresh(self):
        fetch = AsyncMock(return_value=None)
        gate = AsyncMock(return_value="BLOCKED")
        with (
            patch.object(sessions_mod, "_vault_owns_usage", AsyncMock(return_value=True)),
            patch.object(sessions_mod, "reject_if_kiro_unverified", gate),
            patch.object(sessions_mod, "_fetch_usage_bg", fetch),
        ):
            await sessions_mod.api_sessions_usage_refresh(
                _request("POST", "/api/sessions/usage/refresh")
            )
        gate.assert_not_called()
        fetch.assert_awaited_once_with(vault_owned=True)


def _resp(status, body):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = body
    return r


_CREDIT_BODY = {
    "usageBreakdownList": [{"resourceType": "CREDIT", "currentUsage": 42.0, "usageLimit": 1000.0}],
}


class TestFetchWithCredential:
    @pytest.fixture(autouse=True)
    def _clear_caches(self):
        api._PROFILE_ARN_CACHE.clear()
        api._PROFILE_NAME_CACHE.clear()

    def test_uses_stored_arn_and_maps_response(self):
        arn = "arn:aws:codewhisperer:us-east-1:1:profile/p"
        posted = []

        def fake_post(token, target, payload, *, endpoint=None):
            posted.append((target, payload))
            if target == api._TARGET_LIST_PROFILES:
                return _resp(200, {"profiles": [{"arn": arn, "profileName": "Team"}]})
            return _resp(200, _CREDIT_BODY)

        with patch.object(api, "_post", fake_post):
            result = api.fetch_usage_limits_with_credential("FAKE-tok", arn)
        assert result.auth_state == api.AUTH_OK
        assert result.usage["credits_plan"] == 1000.0
        assert result.usage["_profile_arn"] == arn
        assert result.usage["account"] == "Team"
        assert posted[-1] == (api._TARGET_GET_USAGE, {"origin": "AI_EDITOR", "profileArn": arn})

    def test_no_stored_arn_uses_listed_profile_and_its_region(self):
        region = next(r for r in api._RTS_ENDPOINTS if r != api._DEFAULT_RTS_REGION)
        arn = f"arn:aws:codewhisperer:{region}:1:profile/q"

        def fake_post(token, target, payload, *, endpoint=None):
            if target == api._TARGET_LIST_PROFILES:
                return _resp(200, {"profiles": [{"arn": arn}]})
            assert payload["profileArn"] == arn
            assert endpoint == api._RTS_ENDPOINTS[region]
            return _resp(200, _CREDIT_BODY)

        with patch.object(api, "_post", fake_post):
            result = api.fetch_usage_limits_with_credential("FAKE-tok", None)
        assert result.usage["_profile_arn"] == arn

    def test_401_is_auth_rejected(self):
        with patch.object(api, "_post", lambda *a, **k: _resp(401, {})):
            result = api.fetch_usage_limits_with_credential("FAKE-tok", None)
        assert result == api.UsageResult(None, api.AUTH_REJECTED)

    def test_transport_error_is_other_and_never_raises(self):
        def boom(*a, **k):
            raise api._RequestError("dns")

        with patch.object(api, "_post", boom):
            result = api.fetch_usage_limits_with_credential("FAKE-tok", None)
        assert result == api.UsageResult(None, api.AUTH_OTHER)

    def test_empty_token_is_no_credential(self):
        assert api.fetch_usage_limits_with_credential("", None).auth_state == api.AUTH_NO_CREDENTIAL
