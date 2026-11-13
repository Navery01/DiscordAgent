"""
Battle.net account linking, served by aiohttp in the same process (and event
loop) as discord.py.

Flow
----
  /link slash command -> create_link_url() -> private link sent to the player
  GET  /link/start      consent form
  POST /link/consent    records consent, redirects to Battle.net
  GET  /oauth/callback  exchanges the code, saves the player and characters

Register this redirect URI in the Blizzard developer portal, exactly:
    {PUBLIC_BASE_URL}/oauth/callback
"""

from __future__ import annotations

import html
import logging
import os
import secrets
from dataclasses import dataclass
from urllib.parse import urlencode, urlparse

import aiohttp
import asyncpg
import discord
from aiohttp import web

from . import data_tools

log = logging.getLogger("discordagent.link")

REGION = "us"
OAUTH_BASE = "https://oauth.battle.net"
API_BASE = f"https://{REGION}.api.blizzard.com"
SCOPES = "openid wow.profile"
CONSENT_VERSION = "2026-11-v1"  # bump whenever the consent wording changes
MIN_LEVEL = 10  # skip bank alts and fresh level-1s

EXPIRED = (
    "This link has expired or was already used. Run /link in Discord to get a new one."
)


@dataclass(frozen=True)
class Config:
    client_id: str
    client_secret: str
    public_base_url: str  # tunnel hostname, e.g. https://link.yourguild.com

    @property
    def redirect_uri(self) -> str:
        return f"{self.public_base_url}/oauth/callback"

    @classmethod
    def from_env(cls) -> Config:
        public_base_url = normalize_public_base_url(os.environ["PUBLIC_BASE_URL"])
        return cls(
            client_id=os.environ["BNET_CLIENT_ID"],
            client_secret=os.environ["BNET_CLIENT_SECRET"],
            public_base_url=public_base_url,
        )

    @classmethod
    def is_configured(cls) -> bool:
        keys = (
            "DATABASE_URL",
            "BNET_CLIENT_ID",
            "BNET_CLIENT_SECRET",
            "PUBLIC_BASE_URL",
        )
        return all(os.environ.get(k) for k in keys)


def normalize_public_base_url(value: str) -> str:
    """Normalize PUBLIC_BASE_URL and ensure it is a valid HTTP(S) origin."""
    url = value.strip()
    if "://" not in url:
        url = f"http://{url}"

    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(
            "PUBLIC_BASE_URL must be a full URL like https://link.yourguild.com"
        )

    if parsed.path not in {"", "/"} or parsed.params or parsed.query or parsed.fragment:
        raise ValueError(
            "PUBLIC_BASE_URL must only contain scheme and host, for example https://link.yourguild.com"
        )

    return f"{parsed.scheme}://{parsed.netloc}"


# Typed keys for values shared across handlers (aiohttp 3.9+).
BOT = web.AppKey("bot", object)  # the discord.Client, or None in tests
DB = web.AppKey("db", asyncpg.Pool)
CFG = web.AppKey("cfg", Config)
HTTP = web.AppKey("http", aiohttp.ClientSession)


async def create_link_url(pool: asyncpg.Pool, cfg: Config, discord_id: int) -> str:
    """Create a single-use, 10-minute state token and return the player's link."""
    state = secrets.token_urlsafe(32)
    await data_tools.create_link_state(pool, state, discord_id)
    return f"{cfg.public_base_url}/link/start?{urlencode({'state': state})}"


SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",  # keeps ?state and ?code out of Referer headers
    "X-Frame-Options": "DENY",  # nobody can embed the consent form
}

PAGE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  body {{ font: 16px/1.5 system-ui, sans-serif; max-width: 34rem; margin: 3rem auto;
         padding: 0 1rem; color: #1d1d1f; background: #f5f5f7; }}
  .card {{ background: #fff; border: 1px solid #ddd; border-radius: 10px; padding: 1.5rem; }}
  h1 {{ font-size: 1.4rem; margin-top: 0; }}
  button {{ font: inherit; padding: .6rem 1.2rem; border: 0; border-radius: 8px;
           background: #148eff; color: #fff; cursor: pointer; }}
</style>
</head><body><div class="card">{body}</div></body></html>"""


def page(title: str, body_html: str, status: int = 200) -> web.Response:
    """Render a page. body_html must already be escaped where it holds user data."""
    return web.Response(
        text=PAGE.format(title=html.escape(title), body=body_html),
        content_type="text/html",
        status=status,
        headers=SECURITY_HEADERS,
    )


def error_page(message: str, status: int = 400) -> web.Response:
    return page(
        "Something went wrong",
        f"<h1>Something went wrong</h1><p>{html.escape(message)}</p>",
        status,
    )


async def link_start(request: web.Request) -> web.Response:
    state = request.query.get("state", "")
    if not await data_tools.is_state_valid(request.app[DB], state):
        return error_page(EXPIRED)

    body = f"""
<h1>Link your Battle.net account</h1>
<p>Linking lets guildmates find you for raids and dungeons. If you continue, the guild bot stores:</p>
<ul>
  <li>Your Discord ID, Battle.net account ID and BattleTag</li>
  <li>Your WoW characters (level {MIN_LEVEL}+) and their public stats from Raider.IO and Blizzard</li>
  <li>Any roles, interests and availability you add later</li>
</ul>
<p>Guild members can see your characters, roles and availability through the bot.
Run <code>/unlink</code> in Discord at any time to delete all of it.</p>
<form method="post" action="/link/consent">
  <input type="hidden" name="state" value="{html.escape(state)}">
  <p><label><input type="checkbox" name="agree" value="yes" required> I agree</label></p>
  <p><label for="game_version">Game Version</label>
  <select name="game_version" id="game_version" required>
    <option value="retail">Retail</option>
    <option value="classic">Classic</option>
    <option value="forever">Forever</option>
  </select></p>
  <button type="submit">Continue to Battle.net</button>
</form>"""
    return page("Link your Battle.net account", body)


async def link_consent(request: web.Request) -> web.Response:
    form = await request.post()
    state = str(form.get("state", ""))

    if form.get("agree") != "yes":
        return error_page(
            "You need to tick the box to continue. Go back and try again."
        )

    if not await data_tools.record_consent(request.app[DB], state, CONSENT_VERSION):
        return error_page(EXPIRED)

    cfg = request.app[CFG]
    params = {
        "response_type": "code",
        "client_id": cfg.client_id,
        "redirect_uri": cfg.redirect_uri,
        "scope": SCOPES,
        "state": state,  # Battle.net hands this back to the callback unchanged
    }
    # 303 tells the browser to follow with a GET, the right reply to a form POST.
    raise web.HTTPSeeOther(
        f"{OAUTH_BASE}/authorize?{urlencode(params)}", headers=SECURITY_HEADERS
    )


async def oauth_callback(request: web.Request) -> web.Response:
    query = request.query
    if "error" in query:  # the player clicked Deny on Battle.net
        return page(
            "Linking cancelled",
            "<h1>Linking cancelled</h1><p>Nothing was saved. You can close this tab.</p>",
        )

    code, state = query.get("code"), query.get("state")
    if not code or not state:
        return error_page(
            "The link from Battle.net was incomplete. Run /link in Discord to try again."
        )

    db = request.app[DB]

    # Requiring consented_at means nobody can skip the consent form.
    link = await data_tools.claim_state(db, state)
    if link is None:
        return error_page(EXPIRED)

    try:
        token = await exchange_code(request.app, code)
        account = await get_json(request.app[HTTP], f"{OAUTH_BASE}/userinfo", token)
        characters = await fetch_characters(request.app[HTTP], token)
    except (TimeoutError, aiohttp.ClientError):
        log.exception(
            "Battle.net request failed"
        )  # the URLs logged here hold no secrets
        return error_page(
            "Battle.net didn't respond properly. Run /link in Discord to try again.",
            502,
        )
    # The token is dropped here on purpose. It is never stored or logged.

    if not await data_tools.save_link(db, link, account, characters, REGION):
        return error_page(
            "This Battle.net account is already linked to a different "
            "Discord user. Ask an officer if that's wrong.",
            409,
        )

    await notify_player(request.app[BOT], link["discord_id"], len(characters))
    names = "".join(
        f"<li>{html.escape(c['name'])}</li>"
        for c in characters
    )
    body = (
        f"<h1>You're linked</h1><p>Found {len(characters)} characters:</p><ul>{names}</ul>"
        "<p>Head back to Discord to set your main, roles and availability.</p>"
    )
    return page("You're linked", body)


async def exchange_code(app: web.Application, code: str) -> str:
    """Trade the one-time code for a user access token (server-to-server)."""
    cfg = app[CFG]
    async with app[HTTP].post(
        f"{OAUTH_BASE}/token",
        auth=aiohttp.BasicAuth(cfg.client_id, cfg.client_secret),
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": cfg.redirect_uri,
        },
    ) as resp:
        resp.raise_for_status()
        return (await resp.json())["access_token"]


async def get_json(http: aiohttp.ClientSession, url: str, token: str, **params) -> dict:
    async with http.get(
        url, params=params, headers={"Authorization": f"Bearer {token}"}
    ) as resp:
        resp.raise_for_status()
        return await resp.json()


async def fetch_characters(http: aiohttp.ClientSession, token: str) -> list[dict]:
    try:
        data = await get_json(
            http,
            f"{API_BASE}/profile/user/wow",
            token,
            namespace=f"profile-forever-{REGION}",
            locale="en_US",
        )
    except aiohttp.ClientResponseError as e:
        if e.status == 404:  # Battle.net account with no WoW characters in this region
            return []
        raise
    return [
        c
        for acct in data.get("wow_accounts", [])
        for c in acct.get("characters", [])
        if c.get("level", 0) >= MIN_LEVEL
    ]


async def notify_player(bot, discord_id: int, count: int) -> None:
    if bot is None:
        return
    try:
        user = await bot.fetch_user(discord_id)
        await user.send(
            f"Your Battle.net account is linked. I found {count} characters."
        )
    except discord.HTTPException:
        log.info("Couldn't DM %s (DMs closed?)", discord_id)


async def http_session(app: web.Application):
    """One shared outbound HTTP session for the app's lifetime."""
    app[HTTP] = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
    yield
    await app[HTTP].close()


async def start_link_server(
    bot,
    pool: asyncpg.Pool,
    cfg: Config,
    host: str | None = None,
    port: int = 8082,
) -> web.AppRunner:
    # Inside a container the tunnel must reach us on 0.0.0.0; docker-compose
    # only publishes the port on the host's loopback.
    host = host or os.environ.get("LINK_SERVER_HOST", "127.0.0.1")
    port = int(os.environ.get("LINK_SERVER_PORT", str(port)))
    app = web.Application(client_max_size=64 * 1024)  # the only body is a tiny form
    app[BOT] = bot
    app[DB] = pool
    app[CFG] = cfg
    app.cleanup_ctx.append(http_session)
    app.add_routes(
        [
            web.get("/link/start", link_start),
            web.post("/link/consent", link_consent),
            web.get("/oauth/callback", oauth_callback),
        ]
    )
    runner = web.AppRunner(app, access_log=None)  # access logs would record ?code=
    await runner.setup()
    await web.TCPSite(runner, host, port).start()
    log.info("Link server listening on http://%s:%d", host, port)
    return runner
