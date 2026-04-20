from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import asyncpg

from app.config import settings

Mode = str  # 'transcript' | 'short' | 'medium' | 'full'
Lang = str  # 'auto' | 'ru' | 'en' | 'es'


@dataclass
class UserSettings:
    telegram_id: int
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
    ids = settings.admin_ids
    if not ids:
        return
    async with _pool.acquire() as conn:
        await conn.executemany(
            """
            INSERT INTO users (telegram_id, allowed, is_admin)
            VALUES ($1, TRUE, TRUE)
            ON CONFLICT (telegram_id) DO UPDATE
              SET allowed = TRUE, is_admin = TRUE, updated_at = NOW()
            """,
            [(uid,) for uid in ids],
        )


async def get_user(telegram_id: int) -> UserSettings | None:
    assert _pool is not None
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT telegram_id, username, default_mode, default_output_lang, allowed, is_admin "
            "FROM users WHERE telegram_id=$1",
            telegram_id,
        )
    return UserSettings(**dict(row)) if row else None


async def upsert_user(telegram_id: int, username: str | None) -> UserSettings:
    """Insert on first contact (allowed=FALSE by default), update username otherwise."""
    assert _pool is not None
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO users (telegram_id, username)
            VALUES ($1, $2)
            ON CONFLICT (telegram_id) DO UPDATE SET username = EXCLUDED.username
            RETURNING telegram_id, username, default_mode, default_output_lang, allowed, is_admin
            """,
            telegram_id,
            username,
        )
    assert row is not None
    return UserSettings(**dict(row))


async def update_mode(telegram_id: int, mode: Mode) -> None:
    assert _pool is not None
    async with _pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET default_mode=$2, updated_at=NOW() WHERE telegram_id=$1",
            telegram_id,
            mode,
        )


async def update_lang(telegram_id: int, lang: Lang) -> None:
    assert _pool is not None
    async with _pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET default_output_lang=$2, updated_at=NOW() WHERE telegram_id=$1",
            telegram_id,
            lang,
        )


async def grant(telegram_id: int) -> bool:
    """Return True if the user existed and is now allowed; False if the user is unknown."""
    assert _pool is not None
    async with _pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE users SET allowed=TRUE, updated_at=NOW() WHERE telegram_id=$1",
            telegram_id,
        )
    # "UPDATE n"
    return result.endswith(" 1")


async def revoke(telegram_id: int) -> bool:
    assert _pool is not None
    async with _pool.acquire() as conn:
        # Never revoke an admin via this call
        result = await conn.execute(
            "UPDATE users SET allowed=FALSE, updated_at=NOW() "
            "WHERE telegram_id=$1 AND is_admin=FALSE",
            telegram_id,
        )
    return result.endswith(" 1")


async def list_allowed() -> list[UserSettings]:
    assert _pool is not None
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT telegram_id, username, default_mode, default_output_lang, allowed, is_admin "
            "FROM users WHERE allowed=TRUE ORDER BY is_admin DESC, telegram_id"
        )
    return [UserSettings(**dict(r)) for r in rows]
