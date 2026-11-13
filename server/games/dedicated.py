"""
Games played on a dedicated game server.

The game server runs the match's only simulation; players' games connect to it
instead of to each other. The lobby server hands each participant the server's
address and that participant's own token in their `game_launch` arguments
(`/connectserver <host:port> /servertoken <token> /serveruid <id>`), and sends
no peer setup (`HostGame`, `JoinGame`, `ConnectToPeer`) for the game.
"""

import string
from typing import TYPE_CHECKING, NamedTuple

from .custom_game import CustomGame
from .typedefs import InitMode

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
