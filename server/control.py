"""
Tiny http server for introspecting state
"""

import asyncio
import json
import logging
import socket
from typing import TYPE_CHECKING, ClassVar, Optional, cast

from aiohttp import web

from .config import config
from .decorators import with_logger
from .game_service import GameService
from .games import FeaturedModType
from .games.dedicated import (
    DEDICATED_SERVER_REPORTER,
    DedicatedServerGame,
    DedicatedServerJoin,
    launch_dedicated_game
)
from .player_service import PlayerService
from .players import PlayerState

if TYPE_CHECKING:
    from server import ServerInstance


@with_logger
class ControlServer:
    _logger: ClassVar[logging.Logger]

    def __init__(
        self,
        lobby_server: "ServerInstance",
    ):
        self.lobby_server = lobby_server
        self.game_service = cast(GameService, lobby_server.services["game_service"])
        self.player_service = cast(PlayerService, lobby_server.services["player_service"])
        self.host: Optional[str] = None
        self.port: Optional[int] = None

        self.app = web.Application()
        self.runner = web.AppRunner(self.app)

        self.app.add_routes([
            web.get("/games", self.games),
            web.get("/players", self.players),
            web.post("/dedicated_games", self.create_dedicated_game),
            web.post("/dedicated_games/{game_id}/results", self.report_dedicated_results),
        ])
        self._launches: set[asyncio.Task] = set()

    async def run_from_config(self) -> None:
        """
        Initialize the http control server
        """
        host = socket.gethostbyname(socket.gethostname())
        port = config.CONTROL_SERVER_PORT

        await self.shutdown()
        await self.start(host, port)

    async def start(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, host, port)
        await self.site.start()
        self._logger.info(
            "Control server listening on http://%s:%s", host, port
        )

    async def shutdown(self) -> None:
        await self.runner.cleanup()
        self.host = None
        self.port = None

    async def games(self, request: web.Request) -> web.Response:
        return web.json_response([
            game.to_dict()
            for game in self.game_service.all_games
        ])

    async def players(self, request: web.Request) -> web.Response:
        return web.json_response([
            player.to_dict()
            for player in self.player_service.all_players
        ])

    async def create_dedicated_game(self, request: web.Request) -> web.Response:
        """
        Creates a game played on a dedicated game server and launches its
        participants into it. Body:

            {"name": str, "mapname": str, "mod": str (default "faf"),
             "address": "host:port",
             "participants": [{"player_id": int, "token": str,
                               "team": int, "start_spot": int}, ...]}

        Answers 202 with {"game_id": id} once the launch has started. Every
        participant must be online. Off unless `DEDICATED_SERVER_GAMES`.
        """
        if not config.DEDICATED_SERVER_GAMES:
            raise web.HTTPNotFound()
        try:
            body = await request.json()
            participants = body["participants"]
            join = DedicatedServerJoin.validated(
                str(body["address"]),
                {int(p["player_id"]): str(p["token"]) for p in participants},
            )
            if len(join.tokens) != len(participants):
                raise ValueError("a participant is listed twice")
        except (KeyError, TypeError, ValueError) as e:
            return web.json_response({"error": str(e)}, status=400)

        players = []
        for p in participants:
            player = self.player_service.get_player(int(p["player_id"]))
            if player is None or player.lobby_connection is None:
                return web.json_response(
                    {"error": f"player {p['player_id']} is not online"},
                    status=409,
                )
            if player.state is not PlayerState.IDLE:
                return web.json_response(
                    {"error": f"player {p['player_id']} is busy ({player.state.name})"},
                    status=409,
                )
            players.append(player)

        game_map = await self.game_service.get_map(str(body.get("mapname") or "scmp_007"))
        game = self.game_service.create_game(
            game_class=DedicatedServerGame,
            game_mode=str(body.get("mod") or FeaturedModType.FAF).lower(),
            host=players[0],
            name=str(body.get("name") or "Dedicated server game"),
            map=game_map,
            max_players=len(players),
            dedicated_server=join,
        )
        # The game server applies these options; the players' games don't
        # configure the slots of a dedicated-server game, so they count as set.
        for slot, (p, player) in enumerate(zip(participants, players), start=1):
            player.game = game
            game.set_player_option(player.id, "Team", int(p.get("team", 1)))
            game.set_player_option(player.id, "StartSpot", int(p.get("start_spot", slot)))
            game.set_player_option(player.id, "Army", slot)

        task = asyncio.create_task(self._launch(game, players))
        self._launches.add(task)
        task.add_done_callback(self._launches.discard)
        self._logger.info(
            "Dedicated server game %d created for %d participants",
            game.id, len(players),
        )
        return web.json_response({"game_id": game.id}, status=202)

    async def report_dedicated_results(self, request: web.Request) -> web.Response:
        """
        Results and army stats of a dedicated-server game, from its game
        server, the only reporter such a game has. Body:

            {"results": [{"army": int, "result": "victory"|"defeat"|"draw"|...,
                          "score": int, "metadata": [str, ...]}, ...],
             "stats": {"stats": [...]} or the same as a JSON string}

        Either part may be left out; results may come one army at a time.
        Off unless `DEDICATED_SERVER_GAMES`.
        """
        if not config.DEDICATED_SERVER_GAMES:
            raise web.HTTPNotFound()
        try:
            game = self.game_service[int(request.match_info["game_id"])]
        except (KeyError, ValueError):
            game = None
        if not isinstance(game, DedicatedServerGame):
            return web.json_response({"error": "no such dedicated-server game"}, status=404)
        try:
            body = await request.json()
            results = [
                (int(r["army"]), str(r["result"]), int(r.get("score", 0)),
                 frozenset(str(m) for m in r.get("metadata", ())))
                for r in body.get("results", ())
            ]
            stats = body.get("stats")
            if stats is not None and not isinstance(stats, str):
                stats = json.dumps(stats)
        except (KeyError, TypeError, ValueError, AttributeError) as e:
            return web.json_response({"error": str(e)}, status=400)

        for army, result_type, score, metadata in results:
            await game.add_result(DEDICATED_SERVER_REPORTER, army, result_type, score, metadata)
        if stats is not None:
            try:
                game.report_army_stats(stats)
            except (json.JSONDecodeError, KeyError, TypeError) as e:
                return web.json_response({"error": f"stats: {e}"}, status=400)
        return web.json_response({"results": len(results), "stats": stats is not None})

    async def _launch(self, game: DedicatedServerGame, players) -> None:
        try:
            await launch_dedicated_game(game, players)
        except asyncio.TimeoutError:
            self._logger.info("Dedicated server game %d did not launch in time", game.id)
            await game.on_game_finish()
        except Exception:
            self._logger.exception("Dedicated server game %d failed to launch", game.id)
            await game.on_game_finish()
