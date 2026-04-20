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


_pool: asyncpg.Pool | None = None


async def init_pool() -> None:
    global _pool
    _pool = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=5)
    await _run_migrations()


async def close_pool() -> None:
    if _pool:
        await _pool.close()


async def _run_migrations() -> None:
    assert _pool is not None
    sql = Path(__file__).parent.parent.joinpath("migrations", "001_init.sql").read_text()
    async with _pool.acquire() as conn:
        await conn.execute(sql)


async def get_or_create_user(telegram_id: int, username: str | None) -> UserSettings:
    assert _pool is not None
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO users (telegram_id, username)
            VALUES ($1, $2)
            ON CONFLICT (telegram_id) DO UPDATE SET username = EXCLUDED.username
            RETURNING telegram_id, username, default_mode, default_output_lang
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
