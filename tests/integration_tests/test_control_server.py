import asyncio

import aiohttp
import pytest

from server.config import config
from tests.utils import fast_forward

from .conftest import connect_and_sign_in, read_until_command


def listify(obj: dict):
    for k, v in obj.items():
        if isinstance(v, tuple):
            obj[k] = list(v)
        elif isinstance(v, dict):
            obj[k] = listify(v)
    return obj


@fast_forward(2)
async def test_players(control_server, lobby_server, player_service):
    test_id, _, _ = await connect_and_sign_in(
        ("test", "test_password"), lobby_server
    )

    url = f"http://{control_server.host}:{control_server.port}/players"
    async with aiohttp.ClientSession(raise_for_status=True) as session:
        async with session.get(url) as resp:
            data = await resp.json()

            assert data == [listify(player_service[test_id].to_dict())]


@fast_forward(2)
async def test_games(control_server, lobby_server, game_service):
    test_id, _, proto = await connect_and_sign_in(
        ("test", "test_password"), lobby_server
    )
    await proto.send_message({
        "command": "game_host",
        "mod": "faf",
        "visibility": "public"
    })
    msg = await read_until_command(proto, "game_launch")
    await proto.send_message({
        "target": "game",
        "command": "GameState",
        "args": ["Idle"]
    })
    await proto.send_message({
        "target": "game",
        "command": "PlayerOption",
        # Send this as a string because json object keys can only be strings.
        # This makes our assertion check easier.
        "args": [test_id, "Team", "1"]
    })

    url = f"http://{control_server.host}:{control_server.port}/games"
    async with aiohttp.ClientSession(raise_for_status=True) as session:
        async with session.get(url) as resp:
            data = await resp.json()

            assert data == [listify(game_service[msg["uid"]].to_dict())]


async def post_dedicated_game(control_server, body) -> tuple[int, dict]:
    url = f"http://{control_server.host}:{control_server.port}/dedicated_games"
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=body) as resp:
            return resp.status, await resp.json(content_type=None)


@fast_forward(2)
async def test_dedicated_games_off_by_default(control_server):
    status, _ = await post_dedicated_game(control_server, {})
    assert status == 404


@fast_forward(2)
async def test_dedicated_games_bad_request(control_server, monkeypatch):
    monkeypatch.setattr(config, "DEDICATED_SERVER_GAMES", True)

    status, data = await post_dedicated_game(control_server, {
        "address": "no-port",
        "participants": [{"player_id": 1, "token": "abc"}],
    })
    assert status == 400
    assert "host:port" in data["error"]

    status, data = await post_dedicated_game(control_server, {
        "address": "203.0.113.5:27050",
        "participants": [
            {"player_id": 1, "token": "same"},
            {"player_id": 3, "token": "same"},
        ],
    })
    assert status == 400


@fast_forward(2)
async def test_dedicated_games_offline_player(control_server, monkeypatch):
    monkeypatch.setattr(config, "DEDICATED_SERVER_GAMES", True)

    status, data = await post_dedicated_game(control_server, {
        "address": "203.0.113.5:27050",
        "participants": [{"player_id": 1, "token": "tok-1"}],
    })
    assert status == 409


@fast_forward(30)
async def test_dedicated_game_launch(
    control_server, lobby_server, game_service, monkeypatch
):
    monkeypatch.setattr(config, "DEDICATED_SERVER_GAMES", True)
    host_id, _, host_proto = await connect_and_sign_in(
        ("test", "test_password"), lobby_server
    )
    guest_id, _, guest_proto = await connect_and_sign_in(
        ("Rhiza", "puff_the_magic_dragon"), lobby_server
    )
    tokens = {host_id: "host-token-1", guest_id: "guest-token-3"}

    status, data = await post_dedicated_game(control_server, {
        "name": "Server game",
        "mapname": "scmp_009",
        "address": "203.0.113.5:27050",
        "participants": [
            {"player_id": host_id, "token": tokens[host_id], "team": 2},
            {"player_id": guest_id, "token": tokens[guest_id], "team": 3},
        ],
    })
    assert status == 202
    game_id = data["game_id"]

    host_launch = await read_until_command(host_proto, "game_launch", timeout=10)
    assert host_launch["uid"] == game_id
    assert host_launch["args"][-6:] == [
        "/connectserver", "203.0.113.5:27050",
        "/servertoken", tokens[host_id],
        "/serveruid", str(host_id),
    ]
    await host_proto.send_message({"target": "game", "command": "GameState", "args": ["Idle"]})
    await host_proto.send_message({"target": "game", "command": "GameState", "args": ["Lobby"]})

    guest_launch = await read_until_command(guest_proto, "game_launch", timeout=10)
    assert guest_launch["uid"] == game_id
    assert guest_launch["args"][-6:] == [
        "/connectserver", "203.0.113.5:27050",
        "/servertoken", tokens[guest_id],
        "/serveruid", str(guest_id),
    ]
    assert tokens[host_id] not in str(guest_launch)
    assert tokens[guest_id] not in str(host_launch)
    await guest_proto.send_message({"target": "game", "command": "GameState", "args": ["Idle"]})
    await guest_proto.send_message({"target": "game", "command": "GameState", "args": ["Lobby"]})

    # The games connect to the game server themselves: no peer setup.
    for proto in (host_proto, guest_proto):
        with pytest.raises(asyncio.TimeoutError):
            await read_until_command(proto, "HostGame", timeout=2, target="game")
    with pytest.raises(asyncio.TimeoutError):
        await read_until_command(guest_proto, "JoinGame", timeout=2, target="game")
    with pytest.raises(asyncio.TimeoutError):
        await read_until_command(host_proto, "ConnectToPeer", timeout=2, target="game")

    # The game list (game_info) carries no token.
    game_info = str(game_service[game_id].to_dict())
    assert tokens[host_id] not in game_info
    assert tokens[guest_id] not in game_info
