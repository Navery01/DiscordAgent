"""SQLAlchemy ORM models for the Guild Bot OLTP schema.

Mirrors ``src/SCHEMA.sql`` (schema ``guild``, PostgreSQL 14+): 17 tables across
5 areas (reference data, identity/consent, characters/preferences, cached
stats, groups/events) plus operational logging and the ``player_public``
view.

The database objects themselves (schema, enum types, tables, indexes, the
``touch_updated_at`` trigger, roles and grants) are created by
``src/SCHEMA.sql``. These models describe the same structures for use with
SQLAlchemy and are not intended to be used with ``Base.metadata.create_all``
against a fresh database -- enum types are declared with ``create_type=False``
and the trigger/roles/grants have no Python-side equivalent.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime, time
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    LargeBinary,
    MetaData,
    Numeric,
    SmallInteger,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy import Enum as PgEnum
from sqlalchemy.dialects.postgresql import BIGINT, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Declarative base for all ``guild`` schema ORM models."""

    metadata = MetaData(schema="guild")
    # All `timestamptz` columns in SCHEMA.sql map to timezone-aware datetimes.
    type_annotation_map = {datetime: DateTime(timezone=True)}


# ---------------------------------------------------------------------
# Enum types (match the Postgres ENUM types created in SCHEMA.sql)
# ---------------------------------------------------------------------
class WowRegion(str, enum.Enum):
    US = "us"
    EU = "eu"
    KR = "kr"
    TW = "tw"
    CN = "cn"


class WowRole(str, enum.Enum):
    TANK = "tank"
    HEALER = "healer"
    DPS = "dps"


class ActivityType(str, enum.Enum):
    RAID = "raid"
    MPLUS = "mplus"
    LEVELING = "leveling"
    DELVES = "delves"
    PVP = "pvp"


class StatsSource(str, enum.Enum):
    RAIDERIO = "raiderio"
    BLIZZARD = "blizzard"


class EventType(str, enum.Enum):
    RAID = "raid"
    MPLUS = "mplus"
    LEVELING = "leveling"
    DELVES = "delves"
    PVP = "pvp"
    OTHER = "other"


class EventStatus(str, enum.Enum):
    OPEN = "open"
    FULL = "full"
    DONE = "done"
    CANCELLED = "cancelled"


class SignupStatus(str, enum.Enum):
    SIGNED = "signed"
    BENCH = "bench"
    ATTENDED = "attended"
    NO_SHOW = "no_show"


class RaidDifficulty(str, enum.Enum):
    LFR = "lfr"
    NORMAL = "normal"
    HEROIC = "heroic"
    MYTHIC = "mythic"


def _pg_enum(python_enum: type[enum.Enum], name: str) -> PgEnum:
    """Build a ``PgEnum`` bound to an existing Postgres enum type by name."""
    return PgEnum(python_enum, name=name, schema="guild", create_type=False, values_callable=lambda e: [m.value for m in e])


# ---------------------------------------------------------------------
# Reference data (seeded from Blizzard static game data, per season)
# ---------------------------------------------------------------------
class Class(Base):
    __tablename__ = "class"

    class_id: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    name: Mapped[str] = mapped_column(unique=True)

    specs: Mapped[list["Spec"]] = relationship(back_populates="class_")
    characters: Mapped[list["Character"]] = relationship(back_populates="class_")


class Spec(Base):
    __tablename__ = "spec"
    __table_args__ = (UniqueConstraint("class_id", "name"),)

    spec_id: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    class_id: Mapped[int] = mapped_column(ForeignKey("guild.class.class_id"), nullable=False)
    name: Mapped[str]
    role: Mapped[WowRole] = mapped_column(_pg_enum(WowRole, "wow_role"), nullable=False)

    class_: Mapped["Class"] = relationship(back_populates="specs")
    character_stats: Mapped[list["CharacterStats"]] = relationship(back_populates="spec")


class Season(Base):
    __tablename__ = "season"
    __table_args__ = (
        CheckConstraint(
            "ends_at IS NULL OR starts_at IS NULL OR ends_at > starts_at",
            name="season_ends_after_starts",
        ),
    )

    season_id: Mapped[int] = mapped_column(SmallInteger, Identity(always=True), primary_key=True)
    slug: Mapped[str] = mapped_column(unique=True)
    expansion: Mapped[str]
    name: Mapped[str]
    starts_at: Mapped[Optional[datetime]]
    ends_at: Mapped[Optional[datetime]]

    dungeons: Mapped[list["Dungeon"]] = relationship(back_populates="season")
    raids: Mapped[list["Raid"]] = relationship(back_populates="season")
    mplus_runs: Mapped[list["MplusRun"]] = relationship(back_populates="season")


class Dungeon(Base):
    __tablename__ = "dungeon"

    dungeon_id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]
    short_name: Mapped[Optional[str]]
    season_id: Mapped[Optional[int]] = mapped_column(ForeignKey("guild.season.season_id"))

    season: Mapped[Optional["Season"]] = relationship(back_populates="dungeons")
    mplus_runs: Mapped[list["MplusRun"]] = relationship(back_populates="dungeon")
    events: Mapped[list["Event"]] = relationship(back_populates="dungeon")


class Raid(Base):
    __tablename__ = "raid"
    __table_args__ = (CheckConstraint("boss_count > 0", name="raid_boss_count_positive"),)

    raid_id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]
    boss_count: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    season_id: Mapped[Optional[int]] = mapped_column(ForeignKey("guild.season.season_id"))

    season: Mapped[Optional["Season"]] = relationship(back_populates="raids")
    events: Mapped[list["Event"]] = relationship(back_populates="raid")


# ---------------------------------------------------------------------
# Identity and consent (the ONLY place PII lives)
# ---------------------------------------------------------------------
class Player(Base):
    __tablename__ = "player"

    player_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    discord_id: Mapped[int] = mapped_column(BIGINT, unique=True, nullable=False)
    bnet_account_id: Mapped[Optional[int]] = mapped_column(BIGINT, unique=True)
    battletag: Mapped[Optional[str]]
    timezone: Mapped[str] = mapped_column(nullable=False, server_default="America/Chicago")
    consent_version: Mapped[str] = mapped_column(nullable=False)
    consented_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())

    characters: Mapped[list["Character"]] = relationship(back_populates="player", cascade="all, delete-orphan")
    interests: Mapped[list["PlayerInterest"]] = relationship(back_populates="player", cascade="all, delete-orphan")
    availability_windows: Mapped[list["AvailabilityWindow"]] = relationship(
        back_populates="player", cascade="all, delete-orphan"
    )
    organized_events: Mapped[list["Event"]] = relationship(back_populates="organizer")
    agent_query_logs: Mapped[list["AgentQueryLog"]] = relationship(back_populates="player")


class OauthLinkState(Base):
    __tablename__ = "oauth_link_state"
    __table_args__ = (Index("oauth_link_state_expires_idx", "expires_at"),)

    state: Mapped[str] = mapped_column(primary_key=True)
    discord_id: Mapped[int] = mapped_column(BIGINT, nullable=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=text("now() + interval '10 minutes'")
    )


class DeletionLog(Base):
    __tablename__ = "deletion_log"

    discord_id_hash: Mapped[bytes] = mapped_column(LargeBinary, primary_key=True)
    deleted_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())


# ---------------------------------------------------------------------
# Characters and preferences
# ---------------------------------------------------------------------
class Character(Base):
    __tablename__ = "character"
    __table_args__ = (
        UniqueConstraint("region", "realm_slug", "name"),
        Index("character_player_idx", "player_id"),
        Index("character_one_main_idx", "player_id", unique=True, postgresql_where=text("is_main")),
    )

    character_id: Mapped[int] = mapped_column(BIGINT, Identity(always=True), primary_key=True)
    player_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("guild.player.player_id", ondelete="CASCADE"), nullable=False
    )
    blizzard_character_id: Mapped[Optional[int]] = mapped_column(BIGINT, unique=True)
    region: Mapped[WowRegion] = mapped_column(_pg_enum(WowRegion, "wow_region"), nullable=False)
    realm_slug: Mapped[str]
    name: Mapped[str]
    class_id: Mapped[Optional[int]] = mapped_column(ForeignKey("guild.class.class_id"))
    is_main: Mapped[bool] = mapped_column(nullable=False, server_default=text("false"))
    verified: Mapped[bool] = mapped_column(nullable=False, server_default=text("false"))
    linked_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())

    player: Mapped["Player"] = relationship(back_populates="characters")
    class_: Mapped[Optional["Class"]] = relationship(back_populates="characters")
    roles: Mapped[list["CharacterRole"]] = relationship(back_populates="character", cascade="all, delete-orphan")
    stats: Mapped[Optional["CharacterStats"]] = relationship(back_populates="character", cascade="all, delete-orphan")
    mplus_runs: Mapped[list["MplusRun"]] = relationship(back_populates="character", cascade="all, delete-orphan")
    event_signups: Mapped[list["EventSignup"]] = relationship(back_populates="character", cascade="all, delete-orphan")


class CharacterRole(Base):
    __tablename__ = "character_role"
    __table_args__ = (
        CheckConstraint("preference_rank BETWEEN 1 AND 3", name="character_role_preference_rank_range"),
        Index("character_role_role_idx", "role"),
    )

    character_id: Mapped[int] = mapped_column(
        BIGINT, ForeignKey("guild.character.character_id", ondelete="CASCADE"), primary_key=True
    )
    role: Mapped[WowRole] = mapped_column(_pg_enum(WowRole, "wow_role"), primary_key=True)
    preference_rank: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("1"))

    character: Mapped["Character"] = relationship(back_populates="roles")


class PlayerInterest(Base):
    __tablename__ = "player_interest"
    __table_args__ = (CheckConstraint("length(goal) <= 200", name="player_interest_goal_length"),)

    player_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("guild.player.player_id", ondelete="CASCADE"), primary_key=True
    )
    activity: Mapped[ActivityType] = mapped_column(_pg_enum(ActivityType, "activity_type"), primary_key=True)
    goal: Mapped[Optional[str]]

    player: Mapped["Player"] = relationship(back_populates="interests")


class AvailabilityWindow(Base):
    __tablename__ = "availability_window"
    __table_args__ = (
        CheckConstraint("day_of_week BETWEEN 0 AND 6", name="availability_window_day_of_week_range"),
        CheckConstraint("start_local <> end_local", name="availability_window_start_ne_end"),
        Index("availability_window_player_idx", "player_id"),
        Index("availability_window_day_idx", "day_of_week"),
    )

    window_id: Mapped[int] = mapped_column(BIGINT, Identity(always=True), primary_key=True)
    player_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("guild.player.player_id", ondelete="CASCADE"), nullable=False
    )
    day_of_week: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    start_local: Mapped[time] = mapped_column(nullable=False)
    end_local: Mapped[time] = mapped_column(nullable=False)
    activity: Mapped[Optional[ActivityType]] = mapped_column(_pg_enum(ActivityType, "activity_type"))

    player: Mapped["Player"] = relationship(back_populates="availability_windows")


# ---------------------------------------------------------------------
# Cached stats (Raider.IO / Blizzard are the source of truth)
# ---------------------------------------------------------------------
class CharacterStats(Base):
    __tablename__ = "character_stats"

    character_id: Mapped[int] = mapped_column(
        BIGINT, ForeignKey("guild.character.character_id", ondelete="CASCADE"), primary_key=True
    )
    level: Mapped[Optional[int]] = mapped_column(SmallInteger)
    spec_id: Mapped[Optional[int]] = mapped_column(ForeignKey("guild.spec.spec_id"))
    item_level: Mapped[Optional[Decimal]] = mapped_column(Numeric(5, 1))
    mplus_score: Mapped[Optional[Decimal]] = mapped_column(Numeric(7, 1))
    raid_progress: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    source: Mapped[StatsSource] = mapped_column(_pg_enum(StatsSource, "stats_source"), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())

    character: Mapped["Character"] = relationship(back_populates="stats")
    spec: Mapped[Optional["Spec"]] = relationship(back_populates="character_stats")

    __table_args__ = (
        CheckConstraint("level BETWEEN 1 AND 100", name="character_stats_level_range"),
        Index("character_stats_score_idx", mplus_score.desc()),
    )


class MplusRun(Base):
    __tablename__ = "mplus_run"

    run_id: Mapped[int] = mapped_column(BIGINT, Identity(always=True), primary_key=True)
    raiderio_run_id: Mapped[int] = mapped_column(BIGINT, nullable=False)
    character_id: Mapped[int] = mapped_column(
        BIGINT, ForeignKey("guild.character.character_id", ondelete="CASCADE"), nullable=False
    )
    dungeon_id: Mapped[int] = mapped_column(ForeignKey("guild.dungeon.dungeon_id"), nullable=False)
    season_id: Mapped[int] = mapped_column(SmallInteger, ForeignKey("guild.season.season_id"), nullable=False)
    key_level: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    upgrades: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("0"))
    score: Mapped[Optional[Decimal]] = mapped_column(Numeric(6, 1))
    completed_at: Mapped[datetime] = mapped_column(nullable=False)

    character: Mapped["Character"] = relationship(back_populates="mplus_runs")
    dungeon: Mapped["Dungeon"] = relationship(back_populates="mplus_runs")
    season: Mapped["Season"] = relationship(back_populates="mplus_runs")

    __table_args__ = (
        CheckConstraint("key_level >= 2", name="mplus_run_key_level_min"),
        CheckConstraint("upgrades BETWEEN 0 AND 3", name="mplus_run_upgrades_range"),
        UniqueConstraint("raiderio_run_id", "character_id"),
        Index("mplus_run_character_idx", "character_id", "season_id"),
        Index("mplus_run_dungeon_idx", dungeon_id, key_level.desc()),
    )


# ---------------------------------------------------------------------
# Groups and events
# ---------------------------------------------------------------------
class Event(Base):
    __tablename__ = "event"
    __table_args__ = (
        CheckConstraint("length(title) <= 100", name="event_title_length"),
        CheckConstraint("key_level >= 2", name="event_key_level_min"),
        CheckConstraint("dungeon_id IS NULL OR raid_id IS NULL", name="event_dungeon_xor_raid"),
        Index(
            "event_start_idx",
            "scheduled_start",
            postgresql_where=text("status IN ('open', 'full')"),
        ),
    )

    event_id: Mapped[int] = mapped_column(BIGINT, Identity(always=True), primary_key=True)
    type: Mapped[EventType] = mapped_column(_pg_enum(EventType, "event_type"), nullable=False)
    title: Mapped[str] = mapped_column(nullable=False)
    organizer_player_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("guild.player.player_id", ondelete="SET NULL")
    )
    scheduled_start: Mapped[datetime] = mapped_column(nullable=False)
    dungeon_id: Mapped[Optional[int]] = mapped_column(ForeignKey("guild.dungeon.dungeon_id"))
    raid_id: Mapped[Optional[int]] = mapped_column(ForeignKey("guild.raid.raid_id"))
    difficulty: Mapped[Optional[RaidDifficulty]] = mapped_column(_pg_enum(RaidDifficulty, "raid_difficulty"))
    key_level: Mapped[Optional[int]] = mapped_column(SmallInteger)
    status: Mapped[EventStatus] = mapped_column(
        _pg_enum(EventStatus, "event_status"), nullable=False, server_default=EventStatus.OPEN.value
    )
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())

    organizer: Mapped[Optional["Player"]] = relationship(back_populates="organized_events")
    dungeon: Mapped[Optional["Dungeon"]] = relationship(back_populates="events")
    raid: Mapped[Optional["Raid"]] = relationship(back_populates="events")
    signups: Mapped[list["EventSignup"]] = relationship(back_populates="event", cascade="all, delete-orphan")


class EventSignup(Base):
    __tablename__ = "event_signup"
    __table_args__ = (Index("event_signup_character_idx", "character_id"),)

    event_id: Mapped[int] = mapped_column(
        BIGINT, ForeignKey("guild.event.event_id", ondelete="CASCADE"), primary_key=True
    )
    character_id: Mapped[int] = mapped_column(
        BIGINT, ForeignKey("guild.character.character_id", ondelete="CASCADE"), primary_key=True
    )
    role: Mapped[WowRole] = mapped_column(_pg_enum(WowRole, "wow_role"), nullable=False)
    status: Mapped[SignupStatus] = mapped_column(
        _pg_enum(SignupStatus, "signup_status"), nullable=False, server_default=SignupStatus.SIGNED.value
    )
    signed_up_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())

    event: Mapped["Event"] = relationship(back_populates="signups")
    character: Mapped["Character"] = relationship(back_populates="event_signups")


# ---------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------
class AgentQueryLog(Base):
    __tablename__ = "agent_query_log"
    __table_args__ = (
        CheckConstraint("latency_ms >= 0", name="agent_query_log_latency_ms_nonneg"),
        CheckConstraint("tokens >= 0", name="agent_query_log_tokens_nonneg"),
        Index("agent_query_log_created_idx", "created_at"),
    )

    query_id: Mapped[int] = mapped_column(BIGINT, Identity(always=True), primary_key=True)
    player_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("guild.player.player_id", ondelete="SET NULL")
    )
    tool_name: Mapped[str] = mapped_column(nullable=False)
    success: Mapped[bool] = mapped_column(nullable=False)
    latency_ms: Mapped[Optional[int]]
    tokens: Mapped[Optional[int]]
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())

    player: Mapped[Optional["Player"]] = relationship(back_populates="agent_query_logs")


# ---------------------------------------------------------------------
# Agent-facing view: no Discord, Battle.net or BattleTag columns.
# Read-only; maps the `player_public` view defined in SCHEMA.sql.
# ---------------------------------------------------------------------
class PlayerPublic(Base):
    __tablename__ = "player_public"
    __table_args__ = {"info": {"is_view": True}}

    player_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    timezone: Mapped[str]
