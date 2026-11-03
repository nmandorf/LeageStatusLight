"""Offline connection lifecycle tests: no real network or Keychain calls."""

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from aiohttp import ClientResponseError
from hubspaceng.errors import AuthenticationError, HubspaceError
from hubspaceng.models.functions.base import BaseFunction
from hubspaceng.request import HubspaceRequest

from hubspace_connection import HubspaceConnection
from session_cache import SessionCache
from test_light_transport import full_light


def valid_token(value="Bearer offline-old"):
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return value, now + timedelta(minutes=4), now


def metadata():
    device = deepcopy(full_light().device_json)
    device.update(typeId="metadevice.device", children=[])
    device["description"]["device"] = {"deviceClass": "light"}
    return device


def unauthorized():
    return ClientResponseError(
        request_info=Mock(real_url="https://offline.invalid/state"), history=(),
        status=401, message="Offline authentication rejection",
    )


class ConnectionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cache = Mock(spec=SessionCache)
        self.cache.load_token.return_value = None
        self.cache.load_devices.return_value = None
        self.session = Mock()
        self.connection = HubspaceConnection(
            "offline@example.invalid", "offline-password", self.session, self.cache,
        )
        self.api = self.connection.api
        # An accidental unmocked request is a test failure, never a cloud call.
        self.transport = self.enterContext(patch.object(
            HubspaceRequest, "_send_request", new_callable=AsyncMock,
            side_effect=AssertionError("Unexpected network request"),
        ))

    async def test_warm_token_and_device_cache_make_no_startup_requests(self):
        token = valid_token()
        record = {"account_id": "offline-account", "device": metadata()}
        self.cache.load_token.return_value = token
        self.cache.load_devices.return_value = [record]
        with patch.object(self.api, "authenticate", new_callable=AsyncMock) as authenticate, \
                patch.object(self.api, "_get_accounts", new_callable=AsyncMock) as accounts:
            await self.connection.authenticate()
            lights = await self.connection.load_lights()
        self.assertEqual(self.api._security_token, token)
        self.assertTrue(self.connection.used_cached_devices)
        self.assertEqual(len(lights), 1)
        self.assertEqual(lights[0].id, record["device"]["id"])
        self.assertIs(lights[0].api, self.api)
        self.assertEqual(lights[0].account.id, "offline-account")
        self.assertIsNone(lights[0].power.get_state())
        self.assertIsNone(lights[0].brightness.get_state())
        authenticate.assert_not_awaited()
        accounts.assert_not_awaited()
        self.transport.assert_not_awaited()
        self.cache.save_devices.assert_not_called()

    async def test_forced_login_bypasses_and_replaces_cached_token(self):
        self.cache.load_token.return_value = valid_token()
        with patch.object(self.api, "_oauth_authenticate", new_callable=AsyncMock,
                          return_value=("Bearer offline-new", 1200)) as oauth:
            await self.connection.authenticate(force=True)
        self.cache.clear_token.assert_called_once_with()
        self.cache.load_token.assert_not_called()
        oauth.assert_awaited_once_with()
        self.cache.save_token.assert_called_once_with(self.api._security_token)
        self.assertEqual(self.api._security_token[0], "Bearer offline-new")
        self.assertIsNone(self.api._authentication_task)

    async def test_cold_authentication_stores_new_token_without_background_task(self):
        with patch.object(self.api, "_oauth_authenticate", new_callable=AsyncMock,
                          return_value=("Bearer offline-new", 1200)) as oauth:
            await self.connection.authenticate()
        self.cache.load_token.assert_called_once_with()
        oauth.assert_awaited_once_with()
        self.cache.save_token.assert_called_once_with(self.api._security_token)
        self.assertIsNone(self.api._authentication_task)

    async def test_cold_discovery_fetches_metadata_without_per_function_state_reads(self):
        light = metadata()
        unsupported = {"typeId": "metadevice.device", "children": [],
                       "description": {"device": {"deviceClass": "unknown-device"}}}
        combo = {**light, "id": "combo", "children": ["child"]}
        room = {"typeId": "metadevice.room"}
        with patch.object(self.api, "_get_accounts", new_callable=AsyncMock,
                          return_value=[{"account": {"accountId": "offline-account"}}]) as accounts, \
                patch("hubspace_connection.HubspaceAccount._get_metadevices", new_callable=AsyncMock,
                      return_value=[light, unsupported, combo, room]) as devices, \
                patch.object(BaseFunction, "update", new_callable=AsyncMock) as state_read:
            lights = await self.connection.load_lights()
        accounts.assert_awaited_once_with()
        devices.assert_awaited_once_with()
        state_read.assert_not_awaited()
        self.transport.assert_not_awaited()
        self.assertEqual([item.id for item in lights], [light["id"]])
        self.assertFalse(self.connection.used_cached_devices)
        self.cache.save_devices.assert_called_once_with([
            {"account_id": "offline-account", "device": light},
        ])

    async def test_listing_fetches_current_state_and_refreshes_metadata_cache(self):
        light = full_light()
        self.api._accounts = {"offline-account": SimpleNamespace(devices={light.id: light})}
        self.cache.load_devices.return_value = [{"account_id": "stale", "device": metadata()}]
        with patch.object(self.api, "update_accounts", new_callable=AsyncMock) as update:
            lights = await self.connection.load_lights(fresh_state=True)
        self.assertEqual(lights, [light])
        update.assert_awaited_once_with()
        self.cache.load_devices.assert_not_called()
        self.cache.save_devices.assert_called_once_with([
            {"account_id": light.account.id, "device": light.device_json},
        ])
        self.assertFalse(self.connection.used_cached_devices)

    async def test_explicit_refresh_and_malformed_cached_models_use_discovery(self):
        for refresh, records in ((True, []), (False, [{"account_id": "test", "device": {"id": "broken"}}])):
            with self.subTest(refresh=refresh):
                self.cache.reset_mock()
                self.cache.load_devices.return_value = records
                with patch.object(self.api, "_get_accounts", new_callable=AsyncMock, return_value=[]) as accounts:
                    self.assertEqual(await self.connection.load_lights(refresh=refresh), [])
                accounts.assert_awaited_once_with()
                if refresh:
                    self.cache.load_devices.assert_not_called()
                self.cache.save_devices.assert_called_once_with([])
                self.assertFalse(self.connection.used_cached_devices)

    async def test_invalid_metadata_response_does_not_overwrite_cache(self):
        with patch.object(self.api, "_get_accounts", new_callable=AsyncMock,
                          return_value=[{"account": {"accountId": "offline-account"}}]), \
                patch("hubspace_connection.HubspaceAccount._get_metadevices", new_callable=AsyncMock,
                      return_value=None):
            with self.assertRaises(HubspaceError):
                await self.connection.load_lights()
        self.cache.save_devices.assert_not_called()

    async def test_expired_or_near_expiry_token_refreshes_synchronously_and_is_saved(self):
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        for deadline in (None, now - timedelta(seconds=1), now + timedelta(seconds=29)):
            with self.subTest(deadline=deadline):
                self.cache.reset_mock()
                self.api._security_token = ("Bearer offline-old", deadline, now)
                with patch.object(self.api, "_oauth_authenticate", new_callable=AsyncMock,
                                  return_value=("Bearer offline-new", 1200)) as oauth:
                    await self.api._refresh_token()
                oauth.assert_awaited_once_with()
                self.assertEqual(self.api._security_token[0], "Bearer offline-new")
                self.assertIsNone(self.api._authentication_task)
                self.cache.save_token.assert_called_once_with(self.api._security_token)

    async def test_unexpired_token_does_not_refresh(self):
        self.api._security_token = valid_token()
        with patch.object(self.api, "_oauth_authenticate", new_callable=AsyncMock) as oauth:
            await self.api._refresh_token()
        oauth.assert_not_awaited()
        self.cache.save_token.assert_not_called()

    async def test_first_401_reauthenticates_once_and_retry_uses_new_authorization(self):
        self.api._security_token = valid_token()
        response = Mock()
        self.transport.side_effect = [unauthorized(), response]
        with patch.object(self.api, "_oauth_authenticate", new_callable=AsyncMock,
                          return_value=("Bearer offline-new", 1200)) as oauth:
            result = await self.api.request("PUT", "response", "https://offline.invalid/state")
        self.assertEqual(result, (response, None))
        self.assertEqual([call.kwargs["headers"]["Authorization"]
                          for call in self.transport.await_args_list],
                         ["Bearer offline-old", "Bearer offline-new"])
        oauth.assert_awaited_once_with()
        self.cache.save_token.assert_called_once_with(self.api._security_token)
        self.assertIsNone(self.api._authentication_task)

    async def test_second_401_fails_clears_cache_and_does_not_launch_background_auth(self):
        self.api._security_token = valid_token()
        self.transport.side_effect = [unauthorized(), unauthorized()]
        with patch.object(self.api, "_oauth_authenticate", new_callable=AsyncMock,
                          return_value=("Bearer offline-new", 1200)) as oauth:
            with self.assertRaises(AuthenticationError):
                await self.api.request("PUT", "response", "https://offline.invalid/state")
        self.assertEqual(self.transport.await_count, 2)
        oauth.assert_awaited_once_with()
        self.cache.clear_token.assert_called_once_with()
        self.assertIsNone(self.api._security_token[0])
        self.assertIsNone(self.api._authentication_task)

    async def test_login_transport_does_not_add_authorization(self):
        self.api._security_token = valid_token()
        self.transport.side_effect = None
        self.transport.return_value = Mock()
        await self.api.request("POST", "response", "https://offline.invalid/login",
                               headers={"Content-Type": "application/x-www-form-urlencoded"},
                               login_request=True)
        self.assertNotIn("Authorization", self.transport.await_args.kwargs["headers"])

    async def test_authentication_failure_clears_token_and_task_without_saving(self):
        for error in (AuthenticationError("offline rejection"), ValueError("offline bad response")):
            with self.subTest(error=type(error).__name__):
                self.cache.reset_mock()
                self.api._security_token = valid_token()
                with patch.object(self.api, "_oauth_authenticate", new_callable=AsyncMock, side_effect=error):
                    with self.assertRaises(type(error)):
                        await self.api.authenticate()
                self.assertEqual(self.api._security_token, (None, None, None))
                self.cache.clear_token.assert_called_once_with()
                self.cache.save_token.assert_not_called()
                self.assertIsNone(self.api._authentication_task)

    async def test_cancelled_authentication_cancels_inner_task_and_clears_cached_token(self):
        started, finished = asyncio.Event(), asyncio.Event()

        async def pending_oauth():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                finished.set()

        with patch.object(self.api, "_oauth_authenticate", side_effect=pending_oauth):
            task = asyncio.create_task(self.api.authenticate())
            await asyncio.wait_for(started.wait(), timeout=1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(finished.is_set())
        self.assertIsNone(self.api._authentication_task)
        self.assertEqual(self.api._security_token, (None, None, None))
        self.cache.clear_token.assert_called_once_with()
        self.cache.save_token.assert_not_called()


if __name__ == "__main__":
    unittest.main()
