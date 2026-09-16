#!/usr/bin/env python3
"""Print the correct invite URL for this bot, derived from DISCORD_TOKEN.

The most common setup mistake is inviting the bot with only the `bot` scope.
Slash commands then never appear, because registering them needs the
`applications.commands` scope, which is granted at invite time and cannot be
added later from inside the server. Re-inviting with both scopes fixes it -
you do NOT need to kick the bot first, the grant is just updated.
"""
from __future__ import annotations

import base64
import sys

from factchecker.config import ConfigError, load_config

# View Channels | Send Messages | Embed Links | Read Message History
PERMISSIONS = 1024 | 2048 | 16384 | 65536  # 84992


def client_id_from_token(token: str) -> str:
    """The first dot-separated segment of a bot token is the base64 app id."""
    head = token.split(".")[0]
    padded = head + "=" * (-len(head) % 4)
    try:
        decoded = base64.b64decode(padded).decode("utf-8")
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"could not decode the application id from the token ({exc})") from exc
    if not decoded.isdigit():
        raise ValueError("the token does not look like a bot token")
    return decoded


def main() -> int:
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    try:
        client_id = client_id_from_token(config.discord_token)
    except ValueError as exc:
        print(f"{exc}\nCheck DISCORD_TOKEN in .env - it should be the Bot token, "
              "not the client secret or the public key.", file=sys.stderr)
        return 2

    url = (
        "https://discord.com/oauth2/authorize"
        f"?client_id={client_id}"
        "&scope=bot%20applications.commands"
        f"&permissions={PERMISSIONS}"
    )

    print(f"Application ID: {client_id}\n")
    print("Open this to invite (or re-authorise) the bot:\n")
    print(f"  {url}\n")
    print("Both scopes matter:")
    print("  bot                   - lets it join and read/send messages")
    print("  applications.commands - lets it register slash commands\n")
    print("If it is already in the server, opening this again just adds the missing")
    print("scope. Pick the same server and click Authorise; no need to kick it first.")
    print(f"\nThen set DEV_GUILD_ID in .env to that server's ID and restart, so the")
    print("commands register instantly instead of waiting on Discord's global cache.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
