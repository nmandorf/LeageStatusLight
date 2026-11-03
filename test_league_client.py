"""Offline Live Client API tests; no running game or network is required."""

import asyncio
import unittest
from unittest.mock import Mock
from urllib.parse import urlsplit

import aiohttp

from league_client import GameState, LiveGameClient


class FakeResponse:
    def __init__(self, payload=None, *, status=200, error=None):
        self.payload = payload
        self.status = status
        self.error = error
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True

    async def json(self):
        if self.error:
            raise self.error
        return self.payload


def player(name="Local#NA1", dead=False, **extra):
    return {"riotId": name, "isDead": dead, "respawnTimer": 0, **extra}


class LiveGameClientTests(unittest.IsolatedAsyncioTestCase):
    def session_for(self, active, players):
        # A strict session double exposes GET only, preventing any real I/O.
        session = Mock(spec=["get"])
        session.get.side_effect = [FakeResponse(active), FakeResponse(players)]
        return session

    async def sample(self, active, players):
        return await LiveGameClient(self.session_for(active, players)).sample()

    async def test_exact_local_player_dead_and_alive_ignore_other_players(self):
        for dead, expected in ((False, GameState.ALIVE), (True, GameState.DEAD)):
            with self.subTest(dead=dead):
                state = await self.sample("Local#NA1", [
                    player("Other#NA1", not dead),
                    player(dead=dead),
                    player("Local#EUW", not dead),
                ])
                self.assertIs(state, expected)

    async def test_builds_full_riot_id_from_name_and_tag(self):
        self.assertIs(await self.sample("Local#NA1", [{
            "riotIdGameName": "Local", "riotIdTagLine": "NA1", "isDead": True,
        }]), GameState.DEAD)

    async def test_full_and_built_id_on_same_record_count_as_one_player(self):
        self.assertIs(await self.sample("Local#NA1", [player(
            riotIdGameName="Local", riotIdTagLine="NA1",
        )]), GameState.ALIVE)

    async def test_riot_id_takes_precedence_over_legacy_name(self):
        self.assertIs(await self.sample("Local#NA1", [
            player("Other#NA1", dead=True, summonerName="Local#NA1"),
            player(dead=False),
        ]), GameState.ALIVE)

    async def test_legacy_name_fallback_requires_an_exact_unique_match(self):
        self.assertIs(await self.sample("OldLocalName", [
            {"summonerName": "Other", "isDead": False},
            {"summonerName": "OldLocalName", "isDead": True},
        ]), GameState.DEAD)
        self.assertIs(await self.sample("OldLocalName", [
            {"summonerName": "OldLocalName", "isDead": True},
            {"summonerName": "OldLocalName", "isDead": False},
        ]), GameState.UNAVAILABLE)

    async def test_duplicate_riot_id_is_unavailable_without_legacy_fallback(self):
        self.assertIs(await self.sample("Local#NA1", [
            player(dead=True, summonerName="Local#NA1"),
            player(dead=False),
        ]), GameState.UNAVAILABLE)

    async def test_never_uses_partial_case_folded_or_wrong_player_matches(self):
        for active in ("Local", "local#na1", "Absent#NA1"):
            with self.subTest(active=active):
                self.assertIs(await self.sample(active, [player(dead=True)]),
                              GameState.UNAVAILABLE)

    async def test_death_totals_and_respawn_timer_do_not_override_is_dead(self):
        self.assertIs(await self.sample("Local#NA1", [player(
            dead=False, respawnTimer=15, scores={"deaths": 9},
        )]), GameState.ALIVE)
        self.assertIs(await self.sample("Local#NA1", [player(
            dead=True, respawnTimer=0, scores={"deaths": 0},
        )]), GameState.DEAD)

    async def test_malformed_or_absent_active_identity_is_unavailable(self):
        for active in (None, "", "  ", 1, [], {"riotId": "Local#NA1"}):
            with self.subTest(active=active):
                self.assertIs(await self.sample(active, [player()]), GameState.UNAVAILABLE)

    async def test_malformed_or_absent_player_list_is_unavailable(self):
        for players in (None, [], {}, "players", [None], [player(), "bad"]):
            with self.subTest(players=players):
                self.assertIs(await self.sample("Local#NA1", players), GameState.UNAVAILABLE)

    async def test_missing_or_non_boolean_life_state_is_unavailable(self):
        for value in (None, 0, 1, "true", "false", [], {}):
            with self.subTest(value=value):
                self.assertIs(await self.sample("Local#NA1", [player(isDead=value)]),
                              GameState.UNAVAILABLE)
        self.assertIs(await self.sample("Local#NA1", [{"riotId": "Local#NA1"}]),
                      GameState.UNAVAILABLE)

    async def test_reads_only_fixed_loopback_endpoints_with_scoped_tls_exception(self):
        session = self.session_for("Local#NA1", [player()])
        await LiveGameClient(session).sample()
        self.assertEqual(session.get.call_count, 2)
        self.assertEqual([call.args[0] for call in session.get.call_args_list], [
            "https://127.0.0.1:2999/liveclientdata/activeplayername",
            "https://127.0.0.1:2999/liveclientdata/playerlist",
        ])
        for call in session.get.call_args_list:
            url = urlsplit(call.args[0])
            self.assertEqual((url.scheme, url.hostname, url.port), ("https", "127.0.0.1", 2999))
            self.assertEqual(set(call.kwargs), {"ssl", "timeout", "allow_redirects"})
            self.assertIs(call.kwargs["ssl"], False)
            self.assertIs(call.kwargs["allow_redirects"], False)
            self.assertEqual(call.kwargs["timeout"].total, 1.0)

    async def test_rejected_status_including_redirect_is_unavailable(self):
        for endpoint in (0, 1):
            for status in (204, 301, 302, 307, 401, 404, 500):
                with self.subTest(endpoint=endpoint, status=status):
                    responses = [FakeResponse("Local#NA1"), FakeResponse([player()])]
                    responses[endpoint] = FakeResponse(status=status)
                    session = Mock(spec=["get"])
                    session.get.side_effect = responses
                    self.assertIs(await LiveGameClient(session).sample(), GameState.UNAVAILABLE)
                    self.assertEqual(session.get.call_count, endpoint + 1)
                    self.assertTrue(responses[endpoint].closed)

    async def test_connection_refused_timeout_and_bad_json_are_unavailable(self):
        for endpoint in (0, 1):
            for error in (ConnectionRefusedError(), TimeoutError(),
                          aiohttp.ClientConnectionError(), ValueError("bad JSON")):
                with self.subTest(endpoint=endpoint, error=type(error).__name__):
                    session = Mock(spec=["get"])
                    responses = [FakeResponse("Local#NA1"), FakeResponse([player()])]
                    responses[endpoint] = error
                    session.get.side_effect = responses
                    self.assertIs(await LiveGameClient(session).sample(), GameState.UNAVAILABLE)
        session = Mock(spec=["get"])
        session.get.return_value = FakeResponse(error=ValueError("bad JSON"))
        self.assertIs(await LiveGameClient(session).sample(), GameState.UNAVAILABLE)
        self.assertTrue(session.get.return_value.closed)

    async def test_cancellation_propagates(self):
        session = Mock(spec=["get"])
        session.get.return_value = FakeResponse(error=asyncio.CancelledError())
        with self.assertRaises(asyncio.CancelledError):
            await LiveGameClient(session).sample()

    async def test_each_sample_reads_active_identity_again(self):
        session = Mock(spec=["get"])
        session.get.side_effect = [
            FakeResponse("Local#NA1"), FakeResponse([player(dead=True)]),
            FakeResponse("New#NA1"), FakeResponse([player("New#NA1", dead=False)]),
        ]
        client = LiveGameClient(session)
        self.assertIs(await client.sample(), GameState.DEAD)
        self.assertIs(await client.sample(), GameState.ALIVE)
        self.assertEqual(session.get.call_count, 4)


if __name__ == "__main__":
    unittest.main()
