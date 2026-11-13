"""
Games played on a dedicated game server.

The game server runs the match's only simulation; players' games connect to it
instead of to each other. The lobby server hands each participant the server's
address and that participant's own token in their `game_launch` arguments
(`/connectserver <host:port> /servertoken <token> /serveruid <id>`), and sends
no peer setup (`HostGame`, `JoinGame`, `ConnectToPeer`) for the game.
"""

import asyncio
import string
from functools import cached_property
from typing import TYPE_CHECKING, NamedTuple

from .custom_game import CustomGame
from .ladder_game import GameClosedError
from .typedefs import GameState, InitMode

if TYPE_CHECKING:
    from server.players import Player

_TOKEN_CHARS = frozenset(string.printable) - frozenset(string.whitespace)


class DedicatedServerJoin(NamedTuple):
    """Where a dedicated-server game is played and who may join it."""

    # The game server's public "host:port"
    address: str
    # FAF player id -> that player's token. A token is only ever sent to its
    # own player.
    tokens: dict[int, str]

    def launch_args(self, player_id: int) -> list[str]:
        return [
            "/connectserver", self.address,
            "/servertoken", self.tokens[player_id],
            "/serveruid", str(player_id),
        ]

    @classmethod
    def validated(
        cls,
        address: str,
        tokens: dict[int, str],
    ) -> "DedicatedServerJoin":
        host, sep, port = address.rpartition(":")
        if not sep or not host or not port.isdigit() or not 0 < int(port) < 65536:
            raise ValueError(f"address must be host:port, got {address!r}")
        if not tokens:
            raise ValueError("a dedicated-server game needs at least one participant")
        seen = set()
        for player_id, token in tokens.items():
            if not 1 <= len(token) <= 1024 or not set(token) <= _TOKEN_CHARS:
                raise ValueError(
                    f"player {player_id}'s token must be 1 to 1024 printable "
                    "characters without spaces"
                )
            if token in seen:
                raise ValueError("every participant needs a token of their own")
            seen.add(token)
        return cls(address, dict(tokens))


def redact_server_tokens(message):
    """
    A copy of `message` (dicts and lists, any depth) for logging, with every
    value that follows a `/servertoken` argument replaced: tokens never go into
    logs. Returns `message` itself when it carries no token.
    """
    if isinstance(message, dict):
        out = {k: redact_server_tokens(v) for k, v in message.items()}
        return out if any(out[k] is not message[k] for k in message) else message
    if isinstance(message, list):
        if "/servertoken" not in message:
            out = [redact_server_tokens(v) for v in message]
            return out if any(a is not b for a, b in zip(out, message)) else message
        out = list(message)
        for i, v in enumerate(message[:-1]):
            if v == "/servertoken":
                out[i + 1] = "<redacted>"
        return out
    return message


class DedicatedServerGame(CustomGame):
    """
    A custom game whose simulation runs on a dedicated game server. Only the
    participants it was created for are launched into it; nobody joins it
    through `game_join`.
    """
    init_mode = InitMode.AUTO_LOBBY

    def __init__(self, id, *args, dedicated_server: DedicatedServerJoin, **kwargs):
        super().__init__(id, *args, **kwargs)
        self.dedicated_server = dedicated_server

    # Setup waits, as LadderGame's: the launch is driven by the lobby server.
    @cached_property
    def _launch_future(self) -> asyncio.Future:
        return asyncio.get_running_loop().create_future()

    async def wait_hosted(self, timeout: float):
        return await asyncio.wait_for(self._hosted_future, timeout=timeout)

    async def wait_launched(self, timeout: float):
        return await asyncio.wait_for(self._launch_future, timeout=timeout)

    async def launch(self):
        await super().launch()
        self._launch_future.set_result(None)

    async def check_game_finish(self, player):
        if self.state in (GameState.INITIALIZING, GameState.LOBBY):
            if not self._hosted_future.done():
                self._hosted_future.set_exception(GameClosedError(player))
            if not self._launch_future.done():
                self._launch_future.set_exception(GameClosedError(player))
        await super().check_game_finish(player)


async def launch_dedicated_game(
    game: DedicatedServerGame,
    players: list["Player"],
    timeout: float = 60,
) -> None:
    """
    Sends every participant their `game_launch`, the first one as the game's
    nominal host, as the matchmaker does. Raises `asyncio.TimeoutError` when
    the first participant's game doesn't report in time.
    """
    host, *guests = players
    assert host.lobby_connection is not None
    host.lobby_connection.write_launch_game(game, is_host=True)
    await game.wait_hosted(timeout)
    for guest in guests:
        if guest.lobby_connection is not None:
            guest.lobby_connection.write_launch_game(game, is_host=False)
    await game.wait_launched(timeout + 10 * len(guests))
