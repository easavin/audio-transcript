from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import asyncpg

from app.config import settings

Platform = str  # 'telegram' | 'whatsapp'
Mode = str  # 'transcript' | 'short' | 'medium' | 'full'
Lang = str  # 'auto' | 'ru' | 'en' | 'es'

_COLS = "platform, external_id, username, default_mode, default_output_lang, allowed, is_admin"


@dataclass
class UserSettings:
    platform: Platform
    external_id: str
    username: str | None
    default_mode: Mode
    default_output_lang: Lang
    allowed: bool
    is_admin: bool


_pool: asyncpg.Pool | None = None
_MIGRATIONS_DIR = Path(__file__).parent.parent / "migrations"


async def init_pool() -> None:
    global _pool
    _pool = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=5)
    await _run_migrations()
    await _seed_admins()


async def close_pool() -> None:
    if _pool:
        await _pool.close()


async def _run_migrations() -> None:
    assert _pool is not None
    async with _pool.acquire() as conn:
        for sql_file in sorted(_MIGRATIONS_DIR.glob("*.sql")):
            await conn.execute(sql_file.read_text())


async def _seed_admins() -> None:
    assert _pool is not None
    rows = [("telegram", str(uid)) for uid in settings.admin_ids]
    rows += [("whatsapp", wid) for wid in settings.whatsapp_admin_set]
    if not rows:
        return
    async with _pool.acquire() as conn:
        await conn.executemany(
            """
            INSERT INTO users (platform, external_id, allowed, is_admin)
            VALUES ($1, $2, TRUE, TRUE)
            ON CONFLICT (platform, external_id) DO UPDATE
              SET allowed = TRUE, is_admin = TRUE, updated_at = NOW()
            """,
            rows,
        )


async def get_user(platform: Platform, external_id: str) -> UserSettings | None:
    assert _pool is not None
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT {_COLS} FROM users WHERE platform=$1 AND external_id=$2",
            platform,
            external_id,
        )
    return UserSettings(**dict(row)) if row else None


async def upsert_user(
    platform: Platform, external_id: str, username: str | None
) -> UserSettings:
    """Insert on first contact (allowed=FALSE by default), update username otherwise."""
    assert _pool is not None
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            f"""
            INSERT INTO users (platform, external_id, username)
            VALUES ($1, $2, $3)
            ON CONFLICT (platform, external_id) DO UPDATE
              SET username = COALESCE(EXCLUDED.username, users.username)
            RETURNING {_COLS}
            """,
            platform,
            external_id,
            username,
        )
    assert row is not None
    return UserSettings(**dict(row))


async def update_mode(platform: Platform, external_id: str, mode: Mode) -> None:
    assert _pool is not None
    async with _pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET default_mode=$3, updated_at=NOW() "
            "WHERE platform=$1 AND external_id=$2",
            platform,
            external_id,
            mode,
        )


async def update_lang(platform: Platform, external_id: str, lang: Lang) -> None:
    assert _pool is not None
    async with _pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET default_output_lang=$3, updated_at=NOW() "
            "WHERE platform=$1 AND external_id=$2",
            platform,
            external_id,
            lang,
        )


async def grant(platform: Platform, external_id: str) -> bool:
    """Return True if the user existed and is now allowed; False if unknown."""
    assert _pool is not None
    async with _pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE users SET allowed=TRUE, updated_at=NOW() "
            "WHERE platform=$1 AND external_id=$2",
            platform,
            external_id,
        )
    return result.endswith(" 1")


async def revoke(platform: Platform, external_id: str) -> bool:
    assert _pool is not None
    async with _pool.acquire() as conn:
        # Never revoke an admin via this call.
        result = await conn.execute(
            "UPDATE users SET allowed=FALSE, updated_at=NOW() "
            "WHERE platform=$1 AND external_id=$2 AND is_admin=FALSE",
            platform,
            external_id,
        )
    return result.endswith(" 1")


async def list_allowed(platform: Platform | None = None) -> list[UserSettings]:
    assert _pool is not None
    async with _pool.acquire() as conn:
        if platform is None:
            rows = await conn.fetch(
                f"SELECT {_COLS} FROM users WHERE allowed=TRUE "
                "ORDER BY platform, is_admin DESC, external_id"
            )
        else:
            rows = await conn.fetch(
                f"SELECT {_COLS} FROM users WHERE allowed=TRUE AND platform=$1 "
                "ORDER BY is_admin DESC, external_id",
                platform,
            )
    return [UserSettings(**dict(r)) for r in rows]
