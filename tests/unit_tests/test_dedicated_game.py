import pytest

from server.games.dedicated import DedicatedServerJoin, redact_server_tokens


def test_redact_server_tokens():
    payload = {
        "session_id": 3,
        "message": {
            "command": "game_launch",
            "args": ["/numgames", 5, "/connectserver", "203.0.113.5:27050",
                     "/servertoken", "secret", "/serveruid", "1"],
        },
    }

    redacted = redact_server_tokens(payload)

    assert "secret" not in str(redacted)
    assert redacted["message"]["args"][5] == "<redacted>"
    assert redacted["message"]["args"][3] == "203.0.113.5:27050"
    # The message itself is left alone.
    assert payload["message"]["args"][5] == "secret"


def test_redact_server_tokens_without_token_is_identity():
    message = {"command": "game_launch", "args": ["/numgames", 5]}

    assert redact_server_tokens(message) is message


def test_launch_args():
    join = DedicatedServerJoin("203.0.113.5:27050", {1: "a", 2: "b"})

    assert join.launch_args(2) == [
        "/connectserver", "203.0.113.5:27050",
        "/servertoken", "b",
        "/serveruid", "2",
    ]


def test_validated_accepts():
    join = DedicatedServerJoin.validated("game.example.org:27050", {1: "x" * 1024})

    assert join.tokens == {1: "x" * 1024}


@pytest.mark.parametrize("address", [
    "no-port", ":27050", "host:", "host:0", "host:65536", "host:port",
])
def test_validated_rejects_address(address):
    with pytest.raises(ValueError):
        DedicatedServerJoin.validated(address, {1: "a"})


@pytest.mark.parametrize("token", ["", "with space", "tab\there", "x" * 1025, "nl\n"])
def test_validated_rejects_token(token):
    with pytest.raises(ValueError):
        DedicatedServerJoin.validated("203.0.113.5:27050", {1: token})


def test_validated_rejects_shared_token():
    with pytest.raises(ValueError):
        DedicatedServerJoin.validated("203.0.113.5:27050", {1: "same", 2: "same"})


def test_validated_rejects_no_participants():
    with pytest.raises(ValueError):
        DedicatedServerJoin.validated("203.0.113.5:27050", {})
