"""Claude AI agent that orchestrates Discord server setup via tool use."""

import json
import logging

import anthropic
import discord

from . import character_tools, server_tools

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are an expert Discord server architect named Captain Cookie (the murloc boss) specializing in World of Warcraft guild servers. \
Your job is to transform a Discord server into a professional, well-organized WoW guild home.

When asked to set up a server, follow this order:
1. Call get_server_info to understand the current state
2. Create all categories first (top to bottom)
3. Create all text and voice channels (placed in their categories)
4. Create all roles (highest to lowest permission level)
5. Set permissions (channel/category overrides for roles)
6. Send professional messages to key channels (rules, welcome, server-info)

**Discord Naming Rules (important!):**
- Text channel names: lowercase, hyphens instead of spaces, no special chars except - and _
  Good: "rules", "raid-signups", "class-chat", "loot-council"
  Bad: "Rules", "Raid Signups", "🗡️-warriors" (emojis get stripped from text channel names)
- Voice channel names: flexible — mixed case and spaces are fine
  Good: "⚔️ Raid 1", "🏰 Mythic+", "🎙️ Officer Room"
- Category names: flexible — emojis and mixed case work great
  Good: "📋 INFORMATION", "⚔️ RAIDING", "🏰 DUNGEONS", "🔒 OFFICERS"
- Role names: flexible — emojis encouraged
  Good: "👑 Guild Master", "⚔️ Raider", "🌿 Healer", "🛡️ Tank"

**WoW Guild Server Template:**

CATEGORIES & CHANNELS:
- 📋 INFORMATION  (read-only for @everyone)
  - rules              (read-only, topic: "Guild rules and code of conduct")
  - announcements      (read-only, topic: "Guild announcements and news")
  - welcome            (read-only, topic: "Welcome to the guild!")
  - guild-info         (read-only, topic: "About the guild — progression, schedule, goals")
  - patch-notes        (read-only, topic: "WoW patch notes and updates")

- 💬 GENERAL
  - general-chat       (topic: "General guild chatter")
  - introductions      (slowmode: 300s, topic: "Introduce yourself and your character!")
  - off-topic          (topic: "Non-WoW discussion")
  - memes              (topic: "WoW memes and humor")
  - media              (topic: "Screenshots, videos, and streams")

- ⚔️ RAIDING
  - raid-announcements (read-only, topic: "Raid schedules and updates — officers only")
  - raid-signups       (topic: "Sign up for raid nights here")
  - raid-discussion    (topic: "Strategy, comps, and raid prep discussion")
  - loot-council       (topic: "Loot distribution discussion and history")
  - logs-and-parses    (topic: "Share your Warcraft Logs parses")
  - boss-strategies    (topic: "Boss guides and strategy notes")

- 🏰 DUNGEONS & M+
  - mythic-plus-lfg    (topic: "Looking for group for Mythic+ keys")
  - key-listings       (topic: "Post your key level and time")
  - dungeon-discussion (topic: "M+ routes, affixes, and tips")

- 🎮 CLASS CHANNELS  (one per major role — adapt as needed)
  - tanks              (topic: "Tank discussion — specs, gear, cooldowns")
  - healers            (topic: "Healer discussion — specs, gear, throughput")
  - melee-dps          (topic: "Melee DPS discussion")
  - ranged-dps         (topic: "Ranged DPS and caster discussion")

- 🤖 BOTS
  - bot-commands       (topic: "Use bot commands here — WarcraftLogs, Raider.IO, etc.")

- 🔒 OFFICERS  (private — deny view_channel for @everyone)
  - officer-chat       (topic: "Officer discussion")
  - recruitment        (topic: "Reviewing applicants and recruitment strategy")
  - roster-management  (topic: "Roster decisions and attendance tracking")
  - officer-logs       (topic: "Moderation and officer action log")

VOICE CHANNELS (under relevant categories):
- Under ⚔️ RAIDING:
  - ⚔️ Raid 1          (voice)
  - ⚔️ Raid 2          (voice)
  - 📢 Raid Briefing    (voice)

- Under 🏰 DUNGEONS & M+:
  - 🏰 Mythic+ Group 1 (voice)
  - 🏰 Mythic+ Group 2 (voice)

- Under 💬 GENERAL:
  - 🔊 General Voice    (voice)
  - 🎵 Music            (voice)
  - 💤 AFK              (voice)

ROLES (create highest to lowest):
- 👑 Guild Master   color: #f1c40f  permissions: [administrator]
- 🎖️ Officer        color: #e74c3c  permissions: [manage_messages, kick_members, ban_members, manage_roles, view_audit_log]
- ⚔️ Raider         color: #3498db  permissions: [send_messages, view_channel, read_message_history, embed_links, attach_files, add_reactions]
- 🏰 Trial Raider   color: #9b59b6  permissions: [send_messages, view_channel, read_message_history, embed_links, attach_files, add_reactions]
- 🌟 Guild Member   color: #2ecc71  permissions: [send_messages, view_channel, read_message_history, embed_links, attach_files, add_reactions]
- 🤝 Social         color: #95a5a6  permissions: [send_messages, view_channel, read_message_history, embed_links, attach_files, add_reactions]
- 🤖 Bot            color: #7f8c8d  permissions: [send_messages, view_channel, read_message_history, embed_links, attach_files]

KEY PERMISSIONS:
- INFORMATION category: deny send_messages for @everyone
- OFFICERS category: deny view_channel for @everyone; allow for 👑 Guild Master and 🎖️ Officer
- raid-announcements: deny send_messages for @everyone

MESSAGES TO SEND after setup:
- rules: Guild rules (numbered list, 8-10 rules — include raiding attendance, loot etiquette, respectful conduct, voice chat during raids)
- welcome: Warm WoW guild welcome with progression info, raid schedule placeholder, and how to get roles
- guild-info: Guild overview with progression tier, raid days/times, Raider.IO and WarcraftLogs links section

Adapt the structure based on any additional description provided (e.g., casual vs. hardcore, \
current tier/expansion, raid days). Use WoW-specific language and lore references to give it personality.

Work systematically and explain each major phase briefly in your responses."""

TOOLS = [
    {
        "name": "get_server_info",
        "description": "Get current server structure: categories, channels, and roles",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "create_category",
        "description": "Create a channel category",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Category name, e.g. '📋 INFORMATION'",
                },
                "position": {
                    "type": "integer",
                    "description": "Position order (0 = top, omit to append)",
                },
            },
            "required": ["name"],
        },
    },
    {
        "name": "create_text_channel",
        "description": "Create a text channel (names must be lowercase with hyphens, no emojis)",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Channel name: lowercase, hyphens only. E.g. 'rules', 'general-chat'",
                },
                "category": {
                    "type": "string",
                    "description": "Exact category name to place this channel in",
                },
                "topic": {
                    "type": "string",
                    "description": "Channel topic shown in the channel header (1-1024 chars)",
                },
                "slowmode": {
                    "type": "integer",
                    "description": "Slowmode delay in seconds (0 = off, 300 = 5 minutes)",
                },
                "readonly_for_everyone": {
                    "type": "boolean",
                    "description": "If true, @everyone cannot send messages here (for rules/announcements)",
                },
            },
            "required": ["name"],
        },
    },
    {
        "name": "create_voice_channel",
        "description": "Create a voice channel (names can have spaces and emojis)",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Channel name, e.g. '🔊 General' or '🎮 Gaming'",
                },
                "category": {
                    "type": "string",
                    "description": "Exact category name to place this channel in",
                },
                "user_limit": {
                    "type": "integer",
                    "description": "Max users (0 = unlimited)",
                },
            },
            "required": ["name"],
        },
    },
    {
        "name": "create_role",
        "description": "Create a server role with color and permissions",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Role name, e.g. '👑 Admin'"},
                "color_hex": {
                    "type": "string",
                    "description": "Hex color code e.g. '#e74c3c'",
                },
                "hoist": {
                    "type": "boolean",
                    "description": "Show separately in member list (default: true)",
                },
                "mentionable": {
                    "type": "boolean",
                    "description": "Allow @mentioning (default: false)",
                },
                "permissions": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Permission names to grant. Valid values: administrator, manage_channels, "
                        "manage_messages, kick_members, ban_members, manage_roles, send_messages, "
                        "view_channel, embed_links, attach_files, mention_everyone, manage_nicknames, "
                        "view_audit_log, create_instant_invite, add_reactions, read_message_history"
                    ),
                },
            },
            "required": ["name"],
        },
    },
    {
        "name": "set_channel_permissions",
        "description": "Set permission overrides for a role in a specific channel",
        "input_schema": {
            "type": "object",
            "properties": {
                "channel_name": {"type": "string", "description": "Exact channel name"},
                "role_name": {
                    "type": "string",
                    "description": "Exact role name, or '@everyone' for default",
                },
                "allow": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Permissions to explicitly allow in this channel",
                },
                "deny": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Permissions to explicitly deny in this channel",
                },
            },
            "required": ["channel_name", "role_name"],
        },
    },
    {
        "name": "set_category_permissions",
        "description": "Set permission overrides for a role across an entire category",
        "input_schema": {
            "type": "object",
            "properties": {
                "category_name": {
                    "type": "string",
                    "description": "Exact category name",
                },
                "role_name": {
                    "type": "string",
                    "description": "Exact role name, or '@everyone'",
                },
                "allow": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Permissions to explicitly allow",
                },
                "deny": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Permissions to explicitly deny",
                },
            },
            "required": ["category_name", "role_name"],
        },
    },
    {
        "name": "send_message",
        "description": "Send a formatted message to a channel (supports Discord markdown)",
        "input_schema": {
            "type": "object",
            "properties": {
                "channel_name": {"type": "string", "description": "Exact channel name"},
                "content": {
                    "type": "string",
                    "description": (
                        "Message content. Supports Discord markdown: **bold**, *italic*, "
                        "__underline__, ~~strikethrough~~, `code`, ```block```, > quote"
                    ),
                },
            },
            "required": ["channel_name", "content"],
        },
    },
    {
        "name": "update_server",
        "description": "Update server settings (optional)",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "New server name"},
            },
            "required": [],
        },
    },
]

TASK_SYSTEM_PROMPT = """You are an AI assistant with direct control over a World of Warcraft guild Discord server. \
You can read the server's current state and make any changes requested by guild leadership.

You have access to these tools:
- get_server_info — read current categories, channels, and roles
- create_category, create_text_channel, create_voice_channel — create new structure
- create_role — create roles with colors and permissions
- set_channel_permissions, set_category_permissions — manage access control
- send_message — post messages to any channel
- update_server — rename the server
- get_main_character_details — inspect the requesting user's main WoW character (gear, stats, professions)

**Guidelines:**
- Always call get_server_info first if you need to know the current state before acting
- Use exact channel/role names when calling permission or message tools (they must match exactly)
- Text channel names must be lowercase with hyphens (e.g. "raid-signups", not "Raid Signups")
- Voice channel and category names can have spaces and emojis
- If a task is ambiguous, make a reasonable judgment call and explain what you did
- Only do what was asked — don't add unrequested channels or roles
- Keep responses concise; explain what you're doing and confirm when done

**Discord Naming Rules:**
- Text channels: lowercase, hyphens only (e.g. "officer-chat", "loot-council")
- Voice channels: flexible (e.g. "⚔️ Raid 1", "🏰 M+ Group")
- Categories: flexible (e.g. "⚔️ RAIDING", "🔒 OFFICERS")
- Roles: flexible (e.g. "👑 Guild Master", "⚔️ Raider")

Respond naturally and confirm exactly what was done when you finish."""

MAX_ITERATIONS = 60


class ServerSetupAgent:
    def __init__(
        self,
        guild: discord.Guild,
        progress_channel: discord.TextChannel,
        requester_id: int | None = None,
        db=None,
        blizzard=None,
    ):
        self.guild = guild
        self.progress_channel = progress_channel
        self.requester_id = requester_id
        self.db = db
        self.blizzard = blizzard
        self.client = anthropic.AsyncAnthropic()

    async def _post(self, text: str) -> None:
        """Send a progress message, silently ignoring send errors."""
        try:
            if len(text) > 1900:
                text = text[:1900] + "…"
            await self.progress_channel.send(text)
        except discord.DiscordException:
            pass

    async def execute_tool(self, tool_name: str, tool_input: dict) -> str:
        guild = self.guild
        handlers = {
            "get_server_info": lambda: server_tools.get_server_info(guild),
            "create_category": lambda: server_tools.create_category(
                guild,
                tool_input["name"],
                tool_input.get("position"),
            ),
            "create_text_channel": lambda: server_tools.create_text_channel(
                guild,
                tool_input["name"],
                tool_input.get("category"),
                tool_input.get("topic"),
                tool_input.get("slowmode", 0),
                tool_input.get("readonly_for_everyone", False),
            ),
            "create_voice_channel": lambda: server_tools.create_voice_channel(
                guild,
                tool_input["name"],
                tool_input.get("category"),
                tool_input.get("user_limit", 0),
            ),
            "create_role": lambda: server_tools.create_role(
                guild,
                tool_input["name"],
                tool_input.get("color_hex"),
                tool_input.get("hoist", True),
                tool_input.get("mentionable", False),
                tool_input.get("permissions", []),
            ),
            "set_channel_permissions": lambda: server_tools.set_channel_permissions(
                guild,
                tool_input["channel_name"],
                tool_input["role_name"],
                tool_input.get("allow", []),
                tool_input.get("deny", []),
            ),
            "set_category_permissions": lambda: server_tools.set_category_permissions(
                guild,
                tool_input["category_name"],
                tool_input["role_name"],
                tool_input.get("allow", []),
                tool_input.get("deny", []),
            ),
            "send_message": lambda: server_tools.send_message(
                guild,
                tool_input["channel_name"],
                tool_input["content"],
            ),
            "update_server": lambda: server_tools.update_server(
                guild,
                tool_input.get("name"),
            ),
            "get_main_character_details": lambda: character_tools.get_main_character_details(
                self.db,
                self.blizzard,
                self.requester_id,
                tool_input.get("sections"),
            ),
        }

        handler = handlers.get(tool_name)
        if handler is None:
            log.error("Unknown tool called: %s | input: %s", tool_name, tool_input)
            return json.dumps({"success": False, "error": f"Unknown tool: {tool_name}"})

        log.info("Tool call: %s | input: %s", tool_name, json.dumps(tool_input))
        result = await handler()
        log.info("Tool result: %s | output: %s", tool_name, json.dumps(result))
        if not result.get("success"):
            log.warning("Tool failed: %s | error: %s", tool_name, result.get("error"))
        return json.dumps(result)

    async def run(self, description: str | None) -> str:
        """Run the agent loop until the server is set up. Returns a summary string."""
        if description:
            user_message = (
                f"Please set up this Discord server professionally.\n\n"
                f"**Server description:** {description}"
            )
        else:
            user_message = (
                "Please set up this Discord server with a professional general-purpose "
                "community structure."
            )

        log.info(
            "run() started | guild=%s description=%r", self.guild.name, description
        )
        messages = [{"role": "user", "content": user_message}]
        tool_count = 0
        iteration = 0

        while iteration < MAX_ITERATIONS:
            iteration += 1
            log.debug("run() iteration %d", iteration)

            try:
                response = await self.client.messages.create(
                    model="claude-opus-4-6",
                    max_tokens=4096,
                    thinking={"type": "adaptive"},
                    system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
                    tools=TOOLS,
                    messages=messages,
                )
            except anthropic.APIError as e:
                log.error("Anthropic API error on iteration %d: %s", iteration, e)
                raise

            log.debug(
                "stop_reason=%s blocks=%d", response.stop_reason, len(response.content)
            )

            # Post any text the agent produces as a progress update
            for block in response.content:
                if block.type == "text" and block.text.strip():
                    await self._post(f"🤖 {block.text.strip()}")

            # Append full response content (including thinking blocks) to history
            messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "end_turn":
                break

            if response.stop_reason != "tool_use":
                log.warning("Unexpected stop_reason: %s", response.stop_reason)
                break

            # Execute all tool calls in this response
            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue

                tool_count += 1
                result_str = await self.execute_tool(block.name, block.input)
                result_data = json.loads(result_str)

                # Brief inline status
                if result_data.get("success"):
                    note = (
                        " *(already existed)*"
                        if result_data.get("note") == "already exists"
                        else ""
                    )
                    name = (
                        result_data.get("name")
                        or result_data.get("channel")
                        or result_data.get("category")
                        or "✓"
                    )
                    await self._post(f"  `{block.name}` → **{name}**{note}")
                else:
                    await self._post(
                        f"  `{block.name}` ⚠️ {result_data.get('error', 'unknown error')}"
                    )

                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result_str,
                    }
                )

            messages.append({"role": "user", "content": tool_results})

        if iteration >= MAX_ITERATIONS:
            log.error(
                "run() hit MAX_ITERATIONS (%d) | guild=%s",
                MAX_ITERATIONS,
                self.guild.name,
            )
            await self._post("⚠️ Reached step limit — setup may be incomplete.")

        log.info(
            "run() finished | guild=%s tool_count=%d iterations=%d",
            self.guild.name,
            tool_count,
            iteration,
        )

        # Extract the final summary from the last assistant message
        for msg in reversed(messages):
            if msg["role"] == "assistant":
                content = msg["content"]
                if isinstance(content, list):
                    for block in content:
                        if (
                            hasattr(block, "type")
                            and block.type == "text"
                            and block.text.strip()
                        ):
                            return block.text.strip()
                break

        return f"Setup complete — {tool_count} actions performed."

    async def run_task(self, task: str) -> str:
        """Run a single ad-hoc task described in plain English. Returns a summary string."""
        log.info("run_task() started | guild=%s task=%r", self.guild.name, task)
        messages = [{"role": "user", "content": task}]
        tool_count = 0
        iteration = 0

        while iteration < MAX_ITERATIONS:
            iteration += 1
            log.debug("run_task() iteration %d", iteration)

            try:
                response = await self.client.messages.create(
                    model="claude-opus-4-6",
                    max_tokens=2048,
                    thinking={"type": "adaptive"},
                    system=[{"type": "text", "text": TASK_SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
                    tools=[*TOOLS, character_tools.CHARACTER_TOOL],
                    messages=messages,
                )
            except anthropic.APIError as e:
                log.error("Anthropic API error on iteration %d: %s", iteration, e)
                raise

            log.debug(
                "stop_reason=%s blocks=%d", response.stop_reason, len(response.content)
            )

            # The final text is returned to the caller; only post intermediate text
            if response.stop_reason == "tool_use":
                for block in response.content:
                    if block.type == "text" and block.text.strip():
                        await self._post(f"🤖 {block.text.strip()}")

            messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "end_turn":
                break
            if response.stop_reason != "tool_use":
                log.warning("Unexpected stop_reason: %s", response.stop_reason)
                break

            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue

                tool_count += 1
                result_str = await self.execute_tool(block.name, block.input)
                result_data = json.loads(result_str)

                if result_data.get("success"):
                    note = (
                        " *(already existed)*"
                        if result_data.get("note") == "already exists"
                        else ""
                    )
                    name = (
                        result_data.get("name")
                        or result_data.get("channel")
                        or result_data.get("category")
                        or "✓"
                    )
                    await self._post(f"  `{block.name}` → **{name}**{note}")
                else:
                    await self._post(
                        f"  `{block.name}` ⚠️ {result_data.get('error', 'unknown error')}"
                    )

                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result_str,
                    }
                )

            messages.append({"role": "user", "content": tool_results})

        if iteration >= MAX_ITERATIONS:
            log.error(
                "run_task() hit MAX_ITERATIONS (%d) | guild=%s",
                MAX_ITERATIONS,
                self.guild.name,
            )

        log.info(
            "run_task() finished | guild=%s tool_count=%d iterations=%d",
            self.guild.name,
            tool_count,
            iteration,
        )

        # Return the agent's final text response
        for msg in reversed(messages):
            if msg["role"] == "assistant":
                content = msg["content"]
                if isinstance(content, list):
                    for block in content:
                        if (
                            hasattr(block, "type")
                            and block.type == "text"
                            and block.text.strip()
                        ):
                            return block.text.strip()
                break

        return f"Done — {tool_count} action(s) performed."
