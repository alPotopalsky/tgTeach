import logging
import os
from typing import Any

from psycopg_pool import AsyncConnectionPool


CREATE_TABLES = [
    """
    CREATE TABLE IF NOT EXISTS bot_users (
        telegram_user_id BIGINT PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        last_name TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS user_progress (
        telegram_user_id BIGINT PRIMARY KEY
            REFERENCES bot_users (telegram_user_id) ON DELETE CASCADE,
        correct_count INTEGER NOT NULL DEFAULT 0,
        total_answers INTEGER NOT NULL DEFAULT 0,
        correct_answers INTEGER NOT NULL DEFAULT 0,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS answer_events (
        id BIGSERIAL PRIMARY KEY,
        telegram_user_id BIGINT NOT NULL
            REFERENCES bot_users (telegram_user_id) ON DELETE CASCADE,
        expression TEXT NOT NULL,
        selected_answer INTEGER NOT NULL,
        correct_answer INTEGER NOT NULL,
        is_correct BOOLEAN NOT NULL,
        difficulty_level SMALLINT NOT NULL,
        answer_options SMALLINT NOT NULL,
        answered_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS answer_events_user_time_idx
    ON answer_events (telegram_user_id, answered_at DESC)
    """,
]

UPSERT_USER = """
    INSERT INTO bot_users (
        telegram_user_id, username, first_name, last_name
    )
    VALUES (%s, %s, %s, %s)
    ON CONFLICT (telegram_user_id) DO UPDATE SET
        username = EXCLUDED.username,
        first_name = EXCLUDED.first_name,
        last_name = EXCLUDED.last_name,
        updated_at = NOW()
"""

_pool: AsyncConnectionPool | None = None


def is_enabled() -> bool:
    return _pool is not None


async def connect_database(_) -> None:
    global _pool

    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        logging.warning(
            "DATABASE_URL is not set; progress will be stored in memory"
        )
        return

    _pool = AsyncConnectionPool(
        conninfo=database_url,
        min_size=0,
        max_size=5,
        max_idle=60,
        max_lifetime=600,
        check=AsyncConnectionPool.check_connection,
        open=False,
    )
    await _pool.open()

    async with _pool.connection(timeout=30) as connection:
        for statement in CREATE_TABLES:
            await connection.execute(statement)

    logging.info("PostgreSQL connection established and schema is ready")


async def close_database(_) -> None:
    global _pool

    if _pool is not None:
        await _pool.close()
        _pool = None
        logging.info("PostgreSQL connection pool closed")


async def _upsert_user(connection, user: Any) -> None:
    await connection.execute(
        UPSERT_USER,
        (user.id, user.username, user.first_name, user.last_name),
    )


async def get_correct_count(user: Any) -> int:
    if _pool is None:
        return 0

    async with _pool.connection() as connection:
        async with connection.transaction():
            await _upsert_user(connection, user)
            cursor = await connection.execute(
                """
                INSERT INTO user_progress (telegram_user_id)
                VALUES (%s)
                ON CONFLICT (telegram_user_id) DO UPDATE SET
                    updated_at = NOW()
                RETURNING correct_count
                """,
                (user.id,),
            )
            row = await cursor.fetchone()

    return int(row[0])


async def record_answer(
    user: Any,
    expression: str,
    selected_answer: int,
    correct_answer: int,
    difficulty_level: int,
    answer_options: int,
) -> int:
    if _pool is None:
        raise RuntimeError("Database is not connected")

    is_correct = selected_answer == correct_answer
    correct_increment = int(is_correct)

    async with _pool.connection() as connection:
        async with connection.transaction():
            await _upsert_user(connection, user)
            await connection.execute(
                """
                INSERT INTO answer_events (
                    telegram_user_id,
                    expression,
                    selected_answer,
                    correct_answer,
                    is_correct,
                    difficulty_level,
                    answer_options
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    user.id,
                    expression,
                    selected_answer,
                    correct_answer,
                    is_correct,
                    difficulty_level,
                    answer_options,
                ),
            )
            cursor = await connection.execute(
                """
                INSERT INTO user_progress (
                    telegram_user_id,
                    correct_count,
                    total_answers,
                    correct_answers
                )
                VALUES (%s, %s, 1, %s)
                ON CONFLICT (telegram_user_id) DO UPDATE SET
                    correct_count = (
                        user_progress.correct_count
                        + EXCLUDED.correct_count
                    ),
                    total_answers = user_progress.total_answers + 1,
                    correct_answers = (
                        user_progress.correct_answers
                        + EXCLUDED.correct_answers
                    ),
                    updated_at = NOW()
                RETURNING correct_count
                """,
                (user.id, correct_increment, correct_increment),
            )
            row = await cursor.fetchone()

    return int(row[0])
