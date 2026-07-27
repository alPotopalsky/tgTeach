import logging
import os
from typing import Any

from psycopg_pool import AsyncConnectionPool


MATH_LEVEL_COUNT = 10
ENGLISH_LEVEL_COUNT = 10
MATH_CORRECT_TO_ADVANCE = 5
ENGLISH_CORRECT_TO_ADVANCE = 4

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
        math_level SMALLINT NOT NULL DEFAULT 0,
        math_level_correct SMALLINT NOT NULL DEFAULT 0,
        english_level SMALLINT NOT NULL DEFAULT 0,
        english_level_correct SMALLINT NOT NULL DEFAULT 0,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS answer_events (
        id BIGSERIAL PRIMARY KEY,
        telegram_user_id BIGINT NOT NULL
            REFERENCES bot_users (telegram_user_id) ON DELETE CASCADE,
        expression TEXT NOT NULL,
        selected_answer TEXT NOT NULL,
        correct_answer TEXT NOT NULL,
        question_type TEXT NOT NULL DEFAULT 'math',
        is_correct BOOLEAN NOT NULL,
        difficulty_level SMALLINT NOT NULL,
        answer_options SMALLINT NOT NULL,
        response_time_ms INTEGER,
        answered_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    ALTER TABLE answer_events
    ADD COLUMN IF NOT EXISTS response_time_ms INTEGER
    """,
    """
    ALTER TABLE answer_events
    ADD COLUMN IF NOT EXISTS question_type TEXT NOT NULL DEFAULT 'math'
    """,
    """
    ALTER TABLE answer_events
    ALTER COLUMN selected_answer TYPE TEXT USING selected_answer::TEXT
    """,
    """
    ALTER TABLE answer_events
    ALTER COLUMN correct_answer TYPE TEXT USING correct_answer::TEXT
    """,
    """
    ALTER TABLE user_progress
    ADD COLUMN IF NOT EXISTS math_level SMALLINT NOT NULL DEFAULT 0
    """,
    """
    ALTER TABLE user_progress
    ADD COLUMN IF NOT EXISTS math_level_correct SMALLINT NOT NULL DEFAULT 0
    """,
    """
    ALTER TABLE user_progress
    ADD COLUMN IF NOT EXISTS english_level SMALLINT NOT NULL DEFAULT 0
    """,
    """
    ALTER TABLE user_progress
    ADD COLUMN IF NOT EXISTS english_level_correct SMALLINT NOT NULL DEFAULT 0
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


def empty_progress() -> dict[str, int]:
    return {
        "correct_count": 0,
        "total_answers": 0,
        "correct_answers": 0,
        "math_level": 0,
        "math_level_correct": 0,
        "english_level": 0,
        "english_level_correct": 0,
    }


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


def _progress_from_row(row) -> dict[str, int]:
    return {
        "correct_count": int(row[0]),
        "total_answers": int(row[1]),
        "correct_answers": int(row[2]),
        "math_level": int(row[3]),
        "math_level_correct": int(row[4]),
        "english_level": int(row[5]),
        "english_level_correct": int(row[6]),
    }


def advance_progress(
    progress: dict[str, int], question_type: str, is_correct: bool
) -> dict[str, int]:
    progress = progress.copy()
    progress["total_answers"] += 1

    if not is_correct:
        return progress

    progress["correct_count"] += 1
    progress["correct_answers"] += 1

    if question_type == "english":
        level_key = "english_level"
        correct_key = "english_level_correct"
        level_count = ENGLISH_LEVEL_COUNT
        threshold = ENGLISH_CORRECT_TO_ADVANCE
    else:
        level_key = "math_level"
        correct_key = "math_level_correct"
        level_count = MATH_LEVEL_COUNT
        threshold = MATH_CORRECT_TO_ADVANCE

    progress[correct_key] += 1
    if (
        progress[level_key] < level_count - 1
        and progress[correct_key] >= threshold
    ):
        progress[level_key] += 1
        progress[correct_key] = 0

    return progress


async def get_progress(user: Any) -> dict[str, int]:
    if _pool is None:
        return empty_progress()

    async with _pool.connection() as connection:
        async with connection.transaction():
            await _upsert_user(connection, user)
            await connection.execute(
                """
                INSERT INTO user_progress (telegram_user_id)
                VALUES (%s)
                ON CONFLICT (telegram_user_id) DO NOTHING
                """,
                (user.id,),
            )
            cursor = await connection.execute(
                """
                SELECT
                    correct_count,
                    total_answers,
                    correct_answers,
                    math_level,
                    math_level_correct,
                    english_level,
                    english_level_correct
                FROM user_progress
                WHERE telegram_user_id = %s
                """,
                (user.id,),
            )
            row = await cursor.fetchone()

    return _progress_from_row(row)


async def record_answer(
    user: Any,
    expression: str,
    selected_answer: str,
    correct_answer: str,
    question_type: str,
    difficulty_level: int,
    answer_options: int,
    response_time_ms: int,
) -> dict[str, int]:
    if _pool is None:
        raise RuntimeError("Database is not connected")

    is_correct = selected_answer == correct_answer

    async with _pool.connection() as connection:
        async with connection.transaction():
            await _upsert_user(connection, user)
            await connection.execute(
                """
                INSERT INTO user_progress (telegram_user_id)
                VALUES (%s)
                ON CONFLICT (telegram_user_id) DO NOTHING
                """,
                (user.id,),
            )
            await connection.execute(
                """
                INSERT INTO answer_events (
                    telegram_user_id,
                    expression,
                    selected_answer,
                    correct_answer,
                    question_type,
                    is_correct,
                    difficulty_level,
                    answer_options,
                    response_time_ms
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    user.id,
                    expression,
                    selected_answer,
                    correct_answer,
                    question_type,
                    is_correct,
                    difficulty_level,
                    answer_options,
                    response_time_ms,
                ),
            )
            cursor = await connection.execute(
                """
                SELECT
                    correct_count,
                    total_answers,
                    correct_answers,
                    math_level,
                    math_level_correct,
                    english_level,
                    english_level_correct
                FROM user_progress
                WHERE telegram_user_id = %s
                FOR UPDATE
                """,
                (user.id,),
            )
            row = await cursor.fetchone()
            progress = advance_progress(
                _progress_from_row(row), question_type, is_correct
            )

            await connection.execute(
                """
                UPDATE user_progress
                SET
                    correct_count = %s,
                    total_answers = %s,
                    correct_answers = %s,
                    math_level = %s,
                    math_level_correct = %s,
                    english_level = %s,
                    english_level_correct = %s,
                    updated_at = NOW()
                WHERE telegram_user_id = %s
                """,
                (
                    progress["correct_count"],
                    progress["total_answers"],
                    progress["correct_answers"],
                    progress["math_level"],
                    progress["math_level_correct"],
                    progress["english_level"],
                    progress["english_level_correct"],
                    user.id,
                ),
            )

    return progress
