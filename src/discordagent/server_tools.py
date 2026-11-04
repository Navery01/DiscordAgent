"""Discord guild manipulation tools used by the AI agent."""

import discord
from typing import Optional

VALID_PERMISSIONS = {
    "administrator",
    "manage_channels",
    "manage_messages",
    "kick_members",
    "ban_members",
    "manage_roles",
    "send_messages",
    "view_channel",
    "embed_links",
    "attach_files",
    "mention_everyone",
    "manage_nicknames",
    "view_audit_log",
    "create_instant_invite",
    "add_reactions",
    "read_message_history",
    "manage_webhooks",
    "deafen_members",
    "move_members",
    "mute_members",
}


def _build_permissions(permission_names: list) -> discord.Permissions:
    kwargs = {name: True for name in permission_names if name in VALID_PERMISSIONS}
    return discord.Permissions(**kwargs)


def _parse_color(hex_str: str) -> discord.Color:
    try:
        return discord.Color(int(hex_str.lstrip("#"), 16))
    except (ValueError, AttributeError):
        return discord.Color.default()


async def get_server_info(guild: discord.Guild) -> dict:
    categories = []
    for cat in guild.categories:
        channels = [
            {"name": ch.name, "type": str(ch.type)}
            for ch in cat.channels
        ]
        categories.append({"name": cat.name, "channels": channels})

    uncategorized = [
        {"name": ch.name, "type": str(ch.type)}
        for ch in guild.channels
        if ch.category is None and not isinstance(ch, discord.CategoryChannel)
    ]

    roles = [
        {"name": role.name, "color": str(role.color), "hoist": role.hoist}
        for role in guild.roles
        if role.name != "@everyone"
    ]

    return {
        "guild_name": guild.name,
        "member_count": guild.member_count,
        "categories": categories,
        "uncategorized_channels": uncategorized,
        "roles": roles,
    }


async def create_category(
    guild: discord.Guild,
    name: str,
    position: Optional[int] = None,
) -> dict:
    existing = discord.utils.get(guild.categories, name=name)
    if existing:
        return {"success": True, "id": existing.id, "name": existing.name, "note": "already exists"}
    try:
        kwargs = {"name": name}
        if position is not None:
            kwargs["position"] = position
        category = await guild.create_category(**kwargs)
        return {"success": True, "id": category.id, "name": category.name}
    except discord.DiscordException as e:
        return {"success": False, "error": str(e)}


async def create_text_channel(
    guild: discord.Guild,
    name: str,
    category_name: Optional[str] = None,
    topic: Optional[str] = None,
    slowmode: int = 0,
    readonly_for_everyone: bool = False,
) -> dict:
    existing = discord.utils.get(guild.text_channels, name=name)
    if existing:
        return {"success": True, "id": existing.id, "name": existing.name, "note": "already exists"}
    try:
        category = None
        if category_name:
            category = discord.utils.get(guild.categories, name=category_name)

        overwrites = {}
        if readonly_for_everyone:
            overwrites[guild.default_role] = discord.PermissionOverwrite(send_messages=False)

        channel = await guild.create_text_channel(
            name=name,
            category=category,
            topic=topic,
            slowmode_delay=slowmode,
            overwrites=overwrites,
        )
        return {"success": True, "id": channel.id, "name": channel.name}
    except discord.DiscordException as e:
        return {"success": False, "error": str(e)}


async def create_voice_channel(
    guild: discord.Guild,
    name: str,
    category_name: Optional[str] = None,
    user_limit: int = 0,
) -> dict:
    existing = discord.utils.get(guild.voice_channels, name=name)
    if existing:
        return {"success": True, "id": existing.id, "name": existing.name, "note": "already exists"}
    try:
        category = None
        if category_name:
            category = discord.utils.get(guild.categories, name=category_name)

        channel = await guild.create_voice_channel(
            name=name,
            category=category,
            user_limit=user_limit,
        )
        return {"success": True, "id": channel.id, "name": channel.name}
    except discord.DiscordException as e:
        return {"success": False, "error": str(e)}


async def create_role(
    guild: discord.Guild,
    name: str,
    color_hex: Optional[str] = None,
    hoist: bool = True,
    mentionable: bool = False,
    permissions: Optional[list] = None,
) -> dict:
    existing = discord.utils.get(guild.roles, name=name)
    if existing:
        return {"success": True, "id": existing.id, "name": existing.name, "note": "already exists"}
    try:
        kwargs = {"name": name, "hoist": hoist, "mentionable": mentionable}
        if color_hex:
            kwargs["color"] = _parse_color(color_hex)
        if permissions:
            kwargs["permissions"] = _build_permissions(permissions)

        role = await guild.create_role(**kwargs)
        return {"success": True, "id": role.id, "name": role.name}
    except discord.DiscordException as e:
        return {"success": False, "error": str(e)}


async def set_channel_permissions(
    guild: discord.Guild,
    channel_name: str,
    role_name: str,
    allow: Optional[list] = None,
    deny: Optional[list] = None,
) -> dict:
    channel = discord.utils.get(guild.channels, name=channel_name)
    if not channel:
        return {"success": False, "error": f"Channel '{channel_name}' not found"}

    role = guild.default_role if role_name == "@everyone" else discord.utils.get(guild.roles, name=role_name)
    if not role:
        return {"success": False, "error": f"Role '{role_name}' not found"}

    try:
        overwrite = discord.PermissionOverwrite()
        for perm in (allow or []):
            if perm in VALID_PERMISSIONS:
                setattr(overwrite, perm, True)
        for perm in (deny or []):
            if perm in VALID_PERMISSIONS:
                setattr(overwrite, perm, False)

        await channel.set_permissions(role, overwrite=overwrite)
        return {"success": True, "channel": channel_name, "role": role_name}
    except discord.DiscordException as e:
        return {"success": False, "error": str(e)}


async def set_category_permissions(
    guild: discord.Guild,
    category_name: str,
    role_name: str,
    allow: Optional[list] = None,
    deny: Optional[list] = None,
) -> dict:
    category = discord.utils.get(guild.categories, name=category_name)
    if not category:
        return {"success": False, "error": f"Category '{category_name}' not found"}

    role = guild.default_role if role_name == "@everyone" else discord.utils.get(guild.roles, name=role_name)
    if not role:
        return {"success": False, "error": f"Role '{role_name}' not found"}

    try:
        overwrite = discord.PermissionOverwrite()
        for perm in (allow or []):
            if perm in VALID_PERMISSIONS:
                setattr(overwrite, perm, True)
        for perm in (deny or []):
            if perm in VALID_PERMISSIONS:
                setattr(overwrite, perm, False)

        await category.set_permissions(role, overwrite=overwrite)
        return {"success": True, "category": category_name, "role": role_name}
    except discord.DiscordException as e:
        return {"success": False, "error": str(e)}


async def send_message(
    guild: discord.Guild,
    channel_name: str,
    content: str,
) -> dict:
    channel = discord.utils.get(guild.text_channels, name=channel_name)
    if not channel:
        return {"success": False, "error": f"Channel '{channel_name}' not found"}

    try:
        # Split long messages at newlines to stay under Discord's 2000-char limit
        while content:
            if len(content) <= 2000:
                await channel.send(content)
                break
            split_at = content[:2000].rfind("\n")
            if split_at <= 0:
                split_at = 2000
            await channel.send(content[:split_at])
            content = content[split_at:].lstrip()

        return {"success": True, "channel": channel_name}
    except discord.DiscordException as e:
        return {"success": False, "error": str(e)}


async def update_server(
    guild: discord.Guild,
    name: Optional[str] = None,
) -> dict:
    try:
        kwargs = {}
        if name:
            kwargs["name"] = name
        if kwargs:
            await guild.edit(**kwargs)
        return {"success": True}
    except discord.DiscordException as e:
        return {"success": False, "error": str(e)}
