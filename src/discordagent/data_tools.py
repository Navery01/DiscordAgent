import discord
from typing import Optional, Literal
import logging 
import os
import sqlalchemy as sa
import sqlalchemy.ext.asyncio as sa_async
from sqlalchemy.ext.asyncio import AsyncSession
from . import models


def _create_engine(mode: Literal["READ-ONLY", "WRITE"] = "READ-ONLY") -> sa_async.AsyncEngine:
    """Create a SQLAlchemy asynchronous engine.

    Any operations that are used by agents should only use the read-only engine.
    """
    if mode == "READ-ONLY":
        connection_string = os.getenv("READ_ONLY_DATABASE_URL")
    elif mode == "WRITE":
        connection_string = os.getenv("WRITE_DATABASE_URL")
    else:
        raise ValueError(f"Invalid mode: {mode}")
    return sa_async.create_async_engine(connection_string)

async def _test_connection(engine: sa_async.AsyncEngine) -> bool:
    """Test the database connection."""
    try:
        async with engine.connect() as conn:
            await conn.execute(sa.text("SELECT 1"))
        return True
    except Exception as e:
        logging.error("Database connection test failed: %s", e)
        return False

async def get_characters():
    engine = _create_engine()
    async with AsyncSession(engine) as session:
        result = await session.execute(sa.select(models.Character).limit(1000).order_by(models.Character.name))
        return result.scalars().all()

async def get_player_characters(discord_id: int):
    engine = _create_engine()
    async with AsyncSession(engine) as session:
        result = await session.execute(
            sa.select(models.Character)
            .join(models.Player, models.Player.player_id == models.Character.player_id)
            .where(models.Player.discord_id == discord_id)
            .order_by(models.Character.name)
        )
        return result.scalars().all()
    
async def add_character(character: models.Character):
    """Do not grant this function to agents; it requires write access."""
    engine = _create_engine(mode="WRITE")
    async with AsyncSession(engine) as session:
        session.add(character)
        await session.commit()
        await session.refresh(character)
        return character

async def add_player(player: models.Player):
    """Do not grant this function to agents; it requires write access."""
    engine = _create_engine(mode="WRITE")
    async with AsyncSession(engine) as session:
        session.add(player)
        await session.commit()
        await session.refresh(player)
        return player