"""Discord bot entry point and slash commands."""

import logging
import os

import aiohttp
import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

from . import data_tools
from .agent import ServerSetupAgent
from .blizzard_api import BlizzardApiClient
from .link_server import Config, create_link_url, start_link_server

log = logging.getLogger(__name__)


intents = discord.Intents.default()
intents.guilds = True


class DiscordBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.db = None
        self.link_cfg = None
        self.web_runner = None
        self.blizzard = None

    async def setup_hook(self):
        if os.environ.get("BNET_CLIENT_ID") and os.environ.get("BNET_CLIENT_SECRET"):
            self.blizzard = BlizzardApiClient(
                os.environ["BNET_CLIENT_ID"],
                os.environ["BNET_CLIENT_SECRET"],
            )

        if Config.is_configured():
            try:
                self.link_cfg = Config.from_env()
            except ValueError as e:
                log.error("Battle.net linking disabled: %s", e)
            else:
                self.db = await data_tools.create_pool(os.environ["DATABASE_URL"])
                self.web_runner = await start_link_server(self, self.db, self.link_cfg)
                self.purge_link_states.start()
        else:
            log.warning("Battle.net linking disabled: link env vars not set.")
        await self.tree.sync()
        print("Slash commands synced globally.")

    async def close(self):
        if self.db is not None:
            self.purge_link_states.cancel()
            await self.web_runner.cleanup()
            await self.db.close()
        if self.blizzard is not None:
            await self.blizzard.close()
        await super().close()

    @tasks.loop(hours=1)
    async def purge_link_states(self):
        await data_tools.purge_expired_states(self.db)

    async def on_ready(self):
        print(f"Logged in as {self.user} (ID: {self.user.id})")
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name="for /setup",
            )
        )


bot = DiscordBot()


@bot.event
async def on_ready():
    """Triggered when the bot finishes logging in and is ready."""
    for guild in bot.guilds:
        general = discord.utils.get(guild.text_channels, name="general")
        if general:
            await general.send("Mrrrrggghllhlhlh!")


@bot.tree.command(name="link", description="Link your Battle.net account")
async def link(interaction: discord.Interaction):
    if bot.db is None:
        await interaction.response.send_message(
            "Account linking isn't configured.", ephemeral=True
        )
        return
    url = await create_link_url(bot.db, bot.link_cfg, interaction.user.id)
    view = discord.ui.View()
    view.add_item(discord.ui.Button(label="Link Battle.net", url=url))
    try:
        await interaction.response.send_message(
            "Here's your private link. It works once and expires in 10 minutes.",
            view=view,
            ephemeral=True,
        )
    except discord.HTTPException:
        log.exception("Failed to send /link button URL")
        msg = (
            "Discord rejected the button URL, so here's your private link directly:\n"
            f"{url}\n\n"
            "It works once and expires in 10 minutes."
        )
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)


@bot.tree.command(
    name="unlink", description="Delete your linked Battle.net data and characters"
)
async def unlink(interaction: discord.Interaction):
    if bot.db is None:
        await interaction.response.send_message(
            "Account linking isn't configured.", ephemeral=True
        )
        return
    deleted = await data_tools.delete_player(bot.db, interaction.user.id)
    msg = "All your linked data was deleted." if deleted else "You have no linked data."
    await interaction.response.send_message(msg, ephemeral=True)


@bot.tree.command(
    name="character-check",
    description="Fetch your linked character summary from Blizzard",
)
async def character_check(interaction: discord.Interaction):
    if bot.db is None:
        await interaction.response.send_message(
            "Account linking isn't configured.", ephemeral=True
        )
        return
    if bot.blizzard is None:
        await interaction.response.send_message(
            "Blizzard API client credentials aren't configured.", ephemeral=True
        )
        return

    linked = await data_tools.get_player_main_or_first_character(
        bot.db, interaction.user.id
    )
    if linked is None:
        await interaction.response.send_message(
            "No linked character found. Run /link first.", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        summary = await bot.blizzard.get_character_summary(
            linked["region"],
            linked["realm_slug"],
            linked["name"],
        )
    except aiohttp.ClientResponseError as e:
        if e.status == 404:
            await interaction.followup.send(
                "Your linked character is not publicly available in the Blizzard API right now.",
                ephemeral=True,
            )
            return
        log.exception("character-check failed (HTTP %s)", e.status)
        await interaction.followup.send(
            "Blizzard API request failed. Please try again later.", ephemeral=True
        )
        return
    except (aiohttp.ClientError, TimeoutError):
        log.exception("character-check failed")
        await interaction.followup.send(
            "Blizzard API request failed. Please try again later.", ephemeral=True
        )
        return

    level = summary.get("level", "unknown")
    ilvl = summary.get("equipped_item_level", "unknown")
    await interaction.followup.send(
        f"Fetched **{summary.get('name', linked['name'])}** "
        f"({summary.get('realm', {}).get('name', linked['realm_slug'])}) — "
        f"level **{level}**, equipped ilvl **{ilvl}**.",
        ephemeral=True,
    )


@bot.tree.command(
    name="agent",
    description="Ask the AI agent to do anything on the server in plain English",
)
@app_commands.describe(
    agent="What do you want the agent to do? E.g. 'create a channel for alt raids under RAIDING'"
)
async def run_task(interaction: discord.Interaction, agent: str):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message(
            "❌ You need **Administrator** permission to run this command.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(thinking=True)

    guild = interaction.guild
    channel = interaction.channel

    await interaction.followup.send(f"⚙️ **Task:** {agent}")
    log.info(
        "/agent invoked | guild=%s user=%s task=%r", guild.name, interaction.user, agent
    )

    try:
        agent_instance = ServerSetupAgent(guild=guild, progress_channel=channel)
        result = await agent_instance.run_task(agent)
        await channel.send(f"✅ {result}")
    except Exception as e:
        log.exception("/agent failed | guild=%s task=%r", guild.name, agent)
        await channel.send(f"❌ **Error:** {e}")
        raise


@bot.tree.command(
    name="setup",
    description="Use AI to set up the entire server with channels, roles, and messages",
)
@app_commands.describe(
    description=(
        "What kind of server is this? "
        "E.g. 'competitive gaming community', 'Python programming school', 'indie game studio'"
    )
)
async def setup_server(
    interaction: discord.Interaction, description: str | None = None
):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message(
            "❌ You need **Administrator** permission to run this command.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(thinking=True)

    guild = interaction.guild
    channel = interaction.channel

    theme_line = (
        f"**Theme:** {description}"
        if description
        else "**Using default community structure**"
    )

    await interaction.followup.send(
        f"## 🚀 Server Setup Started\n"
        f"{theme_line}\n\n"
        f"The AI agent will now build out your server structure. "
        f"Progress updates will appear below.\n"
        f"*(This usually takes 2–4 minutes)*"
    )
    log.info(
        "/setup invoked | guild=%s user=%s description=%r",
        guild.name,
        interaction.user,
        description,
    )

    try:
        agent = ServerSetupAgent(guild=guild, progress_channel=channel)
        summary = await agent.run(description)

        await channel.send(
            f"## ✅ Setup Complete!\n\n"
            f"{summary}\n\n"
            f"> **Note:** Existing channels and roles were preserved. "
            f"You may want to manually remove any old ones that are no longer needed."
        )

    except Exception as e:
        log.exception(
            "/setup failed | guild=%s description=%r", guild.name, description
        )
        await channel.send(f"❌ **Setup failed:** {e}")
        raise


@bot.tree.command(
    name="setup-preview",
    description="Preview what /setup would do without making any changes",
)
@app_commands.describe(
    description="Describe your server to see a preview of the structure"
)
async def setup_preview(
    interaction: discord.Interaction, description: str | None = None
):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message(
            "❌ You need **Administrator** permission to run this command.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(thinking=True)

    import anthropic

    from .agent import SYSTEM_PROMPT

    client = anthropic.AsyncAnthropic()

    if description:
        user_msg = (
            f"I want to set up a Discord server professionally. "
            f"Server description: {description}\n\n"
            f"Please describe the complete structure you would create (categories, channels, roles, permissions) "
            f"as a detailed plan WITHOUT actually calling any tools. Just list everything out clearly."
        )
    else:
        user_msg = (
            "I want to set up a general Discord server professionally. "
            "Please describe the complete structure you would create (categories, channels, roles, permissions) "
            "as a detailed plan WITHOUT calling any tools. Just list everything out clearly."
        )

    response = await client.messages.create(
        model="claude-opus-4-6",
        max_tokens=2048,
        thinking={"type": "adaptive"},
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
    )

    preview_text = next(
        (b.text for b in response.content if b.type == "text"),
        "Could not generate preview.",
    )

    # Discord has a 2000-char message limit; chunk the preview
    header = "## 📋 Setup Preview\n*Run `/setup` to apply this structure.*\n\n"
    full_text = header + preview_text

    while full_text:
        if len(full_text) <= 2000:
            await interaction.followup.send(full_text)
            break
        split_at = full_text[:2000].rfind("\n")
        if split_at <= 0:
            split_at = 2000
        await interaction.followup.send(full_text[:split_at])
        full_text = full_text[split_at:].lstrip()


def main():
    load_dotenv()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # Quieten noisy libraries
    logging.getLogger("discord").setLevel(logging.WARNING)
    logging.getLogger("anthropic").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise ValueError(
            "DISCORD_TOKEN environment variable is not set.\n"
            "Create a .env file with:\n"
            "  DISCORD_TOKEN=your_bot_token_here\n"
            "  ANTHROPIC_API_KEY=your_anthropic_key_here"
        )
    bot.run(token)
