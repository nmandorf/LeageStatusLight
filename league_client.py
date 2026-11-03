"""Read only the local player's current life state from Riot's Live Client API.

The running League game exposes this API on loopback with a self-signed TLS
certificate. Certificate verification is disabled on these two fixed loopback
requests only; redirects are refused so that exception cannot reach another
host. No Riot account credentials, lockfile, or developer API key are needed.
"""

from enum import Enum

import aiohttp


class GameState(str, Enum):
    ALIVE = "alive"
    DEAD = "dead"
    UNAVAILABLE = "unavailable"


_ACTIVE_PLAYER_URL = "https://127.0.0.1:2999/liveclientdata/activeplayername"
_PLAYER_LIST_URL = "https://127.0.0.1:2999/liveclientdata/playerlist"
_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=1.0)


def _riot_ids(player: dict) -> set[str]:
    """Accept the full Riot ID and its documented name/tag representation."""
    result = set()
    full_id = player.get("riotId")
    if isinstance(full_id, str) and full_id:
        result.add(full_id)
    name, tag = player.get("riotIdGameName"), player.get("riotIdTagLine")
    if isinstance(name, str) and name and isinstance(tag, str) and tag:
        result.add(f"{name}#{tag}")
    return result


def _player_state(active_name, players) -> GameState:
    if not isinstance(active_name, str) or not active_name.strip():
        return GameState.UNAVAILABLE
    if not isinstance(players, list) or not all(isinstance(p, dict) for p in players):
        return GameState.UNAVAILABLE

    matches = [player for player in players if active_name in _riot_ids(player)]
    if not matches:
        # Older game versions return the legacy summonerName. Never use a
        # partial Riot ID, display-name-only match, or the first player.
        matches = [player for player in players if player.get("summonerName") == active_name]
    if len(matches) != 1:
        return GameState.UNAVAILABLE

    is_dead = matches[0].get("isDead")
    if type(is_dead) is not bool:
        return GameState.UNAVAILABLE
    # Death totals and respawnTimer can remain nonzero after a respawn. The
    # player's explicit current isDead flag is the only state signal used.
    return GameState.DEAD if is_dead else GameState.ALIVE


class LiveGameClient:
    """Poll with a caller-owned aiohttp session created with trust_env=False."""

    def __init__(self, session: aiohttp.ClientSession):
        self._session = session

    async def sample(self) -> GameState:
        """Return UNAVAILABLE when the game or its exact local player is absent.

        Each sample reads the active identity again so a previous game/player
        cannot accidentally be reused. Cancellation is allowed to propagate.
        """
        try:
            async with self._session.get(
                _ACTIVE_PLAYER_URL,
                timeout=_REQUEST_TIMEOUT,
                ssl=False,
                allow_redirects=False,
            ) as response:
                if response.status != 200:
                    return GameState.UNAVAILABLE
                active_name = await response.json()

            async with self._session.get(
                _PLAYER_LIST_URL,
                timeout=_REQUEST_TIMEOUT,
                ssl=False,
                allow_redirects=False,
            ) as response:
                if response.status != 200:
                    return GameState.UNAVAILABLE
                players = await response.json()
        except (aiohttp.ClientError, TimeoutError, OSError, ValueError):
            # An unavailable game is ordinary between matches. Do not log
            # response payloads or player identities from the local API.
            return GameState.UNAVAILABLE

        return _player_state(active_name, players)
