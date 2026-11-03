"""Reuse a login and light definitions between short terminal commands.

Compatibility code in this module targets hubspace-ng 0.0.4. It deliberately
keeps the upstream request lock: concurrent cloud requests can time out.
"""

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

from hubspaceng.account import HubspaceAccount
from hubspaceng.api import API
from hubspaceng.errors import HubspaceError
from hubspaceng.models.devices.lights.base import BaseLightDevice
from hubspaceng.request import HubspaceRequest


class CurrentTokenRequest(HubspaceRequest):
    """Use the new token when upstream retries a request rejected with 401."""

    def __init__(self, session, api):
        super().__init__(session)
        self.api = api

    async def _send_request(self, *args, headers=None, **kwargs):
        headers = dict(headers or {})
        if "Authorization" in headers:
            headers["Authorization"] = self.api._security_token[0]
        return await super()._send_request(*args, headers=headers, **kwargs)


class CachedAPI(API):
    def __init__(self, email, password, session, cache):
        super().__init__(email, password, websession=session)
        self.cache = cache
        self._hsrequests = CurrentTokenRequest(session, self)

    async def _refresh_token(self):
        token, deadline, _ = self._security_token
        if not token or not deadline or deadline <= datetime.utcnow() + timedelta(seconds=30):
            await self.authenticate(wait=True)

    async def authenticate(self, wait=True):
        # The only wait=False path left is upstream's second failed 401. Do
        # not launch a third login in the background as the process exits.
        if not wait:
            self.cache.clear_token()
            return None
        try:
            await super().authenticate(wait=True)
        except BaseException:
            self._security_token = (None, None, None)
            self.cache.clear_token()
            raise
        finally:
            task = self._authentication_task
            if task is not None:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                self._authentication_task = None
        self.cache.save_token(self._security_token)


class HubspaceConnection:
    def __init__(self, email, password, session, cache):
        self.cache = cache
        self.api = CachedAPI(email, password, session, cache)
        self.used_cached_devices = False

    async def authenticate(self, force=False):
        if force:
            self.cache.clear_token()
        token = None if force else self.cache.load_token()
        if token is not None:
            self.api._security_token = token
        else:
            await self.api.authenticate(wait=True)

    def _restore_lights(self, records):
        return [
            BaseLightDevice(
                record["device"],
                SimpleNamespace(id=record["account_id"], api=self.api),
                None,
            )
            for record in records
        ]

    async def load_lights(self, fresh_state=False, refresh=False):
        """List/login fetch state; write commands only need device definitions."""
        self.used_cached_devices = False
        if not fresh_state and not refresh:
            records = self.cache.load_devices()
            if records is not None:
                try:
                    lights = self._restore_lights(records)
                except (AttributeError, KeyError, TypeError, ValueError):
                    pass  # Invalid metadata must not stop a normal discovery.
                else:
                    self.used_cached_devices = True
                    return lights

        if fresh_state:
            await self.api.update_accounts()
            lights = [d for d in self.api.devices.values() if isinstance(d, BaseLightDevice)]
        else:
            lights = []
            for account_json in await self.api._get_accounts():
                account = HubspaceAccount(self.api, account_json)
                if not account.id:
                    continue
                devices = await account._get_metadevices()
                if not isinstance(devices, list):
                    raise HubspaceError("Expected a list of Hubspace devices.")
                # Only construct leaf lights. Other device types are irrelevant
                # to these commands and older upstream parsers may reject them.
                for device in devices:
                    if (device.get("typeId") == "metadevice.device"
                            and not device.get("children")
                            and device.get("description", {}).get("device", {}).get("deviceClass") == "light"):
                        lights.append(BaseLightDevice(device, account, None))

        self.cache.save_devices([
            {"account_id": light.account.id, "device": light.device_json}
            for light in lights
        ])
        return lights
