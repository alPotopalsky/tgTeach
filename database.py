import logging
import os
from statistics import median
from typing import Any

from psycopg_pool import AsyncConnectionPool


SUBJECTS = ("math", "english")
LEVEL_COUNT = 10
START_LEVEL = 5
CALIBRATION_CORRECT = 5
TIMING_WINDOW = 20
ADAPTATION_WINDOW = 5
TRUSTED_RESPONSE_LIMIT_MS = 15_000
SUSPICIOUS_RESPONSE_LIMIT_MS = 20_000
MIN_BONUS_TARGET_SECONDS = 5
MAX_BONUS_TARGET_SECONDS = 15

MAIN_SCHEMA = [
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
    CREATE TABLE IF NOT EXISTS user_subject_progress (
        telegram_user_id BIGINT NOT NULL
            REFERENCES bot_users (telegram_user_id) ON DELETE CASCADE,
        subject TEXT NOT NULL,
        level SMALLINT NOT NULL DEFAULT 5
            CHECK (level BETWEEN 1 AND 10),
        calibration_correct SMALLINT NOT NULL DEFAULT 0,
        trusted_times_ms INTEGER[] NOT NULL DEFAULT '{}',
        recent_results TEXT[] NOT NULL DEFAULT '{}',
        total_answers INTEGER NOT NULL DEFAULT 0,
        correct_answers INTEGER NOT NULL DEFAULT 0,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        PRIMARY KEY (telegram_user_id, subject)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS user_subject_progress_subject_idx
    ON user_subject_progress (subject, level)
    """,
]

LOG_SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS log_users (
        telegram_user_id BIGINT PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        last_name TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS answer_logs (
        id BIGSERIAL PRIMARY KEY,
        telegram_user_id BIGINT NOT NULL
            REFERENCES log_users (telegram_user_id) ON DELETE CASCADE,
        subject TEXT NOT NULL,
        task_id INTEGER NOT NULL,
        expression TEXT NOT NULL,
        selected_answer TEXT NOT NULL,
        correct_answer TEXT NOT NULL,
        is_correct BOOLEAN NOT NULL,
        first_attempt BOOLEAN NOT NULL,
        affects_progress BOOLEAN NOT NULL,
        difficulty_level SMALLINT NOT NULL,
        answer_options SMALLINT NOT NULL,
        response_time_ms INTEGER NOT NULL,
        timing_class TEXT NOT NULL,
        is_suspicious BOOLEAN NOT NULL,
        earned_bonus BOOLEAN NOT NULL,
        bonus_target_seconds SMALLINT,
        answered_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS answer_logs_user_time_idx
    ON answer_logs (telegram_user_id, answered_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS answer_logs_answered_at_idx
    ON answer_logs (answered_at)
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

_progress_pool: AsyncConnectionPool | None = None
_log_pool: AsyncConnectionPool | None = None
_log_pool_is_shared = False


def is_enabled() -> bool:
    return _progress_pool is not None


def empty_subject_progress() -> dict[str, Any]:
    return {
        "level": START_LEVEL,
        "calibration_correct": 0,
        "trusted_times_ms": [],
        "recent_results": [],
        "total_answers": 0,
        "correct_answers": 0,
    }


def empty_progress() -> dict[str, dict[str, dict[str, Any]]]:
    return {
        "subjects": {
            subject: empty_subject_progress() for subject in SUBJECTS
        }
    }


def get_subject_progress(
    progress: dict[str, Any], subject: str
) -> dict[str, Any]:
    subjects = progress.setdefault("subjects", {})
    return subjects.setdefault(subject, empty_subject_progress())


def bonus_target_seconds(subject_progress: dict[str, Any]) -> int | None:
    times = subject_progress.get("trusted_times_ms", [])
    if (
        subject_progress.get("calibration_correct", 0)
        < CALIBRATION_CORRECT
        or len(times) < CALIBRATION_CORRECT
    ):
        return None

    personal_median_ms = int(median(times))
    rounded_seconds = ((personal_median_ms + 2_500) // 5_000) * 5
    return max(
        MIN_BONUS_TARGET_SECONDS,
        min(MAX_BONUS_TARGET_SECONDS, rounded_seconds),
    )


def classify_response_time(
    subject_progress: dict[str, Any], response_time_ms: int
) -> str:
    if response_time_ms > SUSPICIOUS_RESPONSE_LIMIT_MS:
        return "suspicious"

    times = subject_progress.get("trusted_times_ms", [])
    calibrated = (
        subject_progress.get("calibration_correct", 0)
        >= CALIBRATION_CORRECT
        and len(times) >= CALIBRATION_CORRECT
    )
    if calibrated:
        personal_slow_limit = min(
            TRUSTED_RESPONSE_LIMIT_MS,
            round(median(times) * 1.5),
        )
    else:
        personal_slow_limit = TRUSTED_RESPONSE_LIMIT_MS

    if response_time_ms > personal_slow_limit:
        return "slow"
    return "normal"


def apply_answer_to_progress(
    progress: dict[str, Any],
    subject: str,
    is_correct: bool,
    response_time_ms: int,
    first_attempt: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    progress = {
        "subjects": {
            name: {
                **state,
                "trusted_times_ms": list(state.get("trusted_times_ms", [])),
                "recent_results": list(state.get("recent_results", [])),
            }
            for name, state in progress.get("subjects", {}).items()
        }
    }
    state = get_subject_progress(progress, subject)
    was_calibrated = (
        state["calibration_correct"] >= CALIBRATION_CORRECT
    )
    target_seconds = bonus_target_seconds(state)
    timing_class = classify_response_time(state, response_time_ms)
    affects_progress = first_attempt
    earned_bonus = bool(
        first_attempt
        and is_correct
        and target_seconds is not None
        and response_time_ms <= target_seconds * 1_000
    )

    if first_attempt:
        state["total_answers"] += 1
        if is_correct:
            state["correct_answers"] += 1

    trusted_for_timing = bool(
        first_attempt
        and is_correct
        and response_time_ms <= SUSPICIOUS_RESPONSE_LIMIT_MS
    )
    if trusted_for_timing:
        state["trusted_times_ms"].append(response_time_ms)
        state["trusted_times_ms"] = state["trusted_times_ms"][
            -TIMING_WINDOW:
        ]
        state["calibration_correct"] = min(
            CALIBRATION_CORRECT,
            state["calibration_correct"] + 1,
        )

    level_change = 0
    if affects_progress:
        if not is_correct:
            result = "wrong"
        elif timing_class == "suspicious":
            result = "suspicious"
        elif was_calibrated and timing_class == "slow":
            result = "slow"
        else:
            result = "confident"

        state["recent_results"].append(result)
        state["recent_results"] = state["recent_results"][
            -ADAPTATION_WINDOW:
        ]
        recent = state["recent_results"]

        if len(recent) == ADAPTATION_WINDOW:
            confident_count = recent.count("confident")
            wrong_count = recent.count("wrong")
            slow_count = recent.count("slow") + recent.count("suspicious")

            if confident_count >= 4 and state["level"] < LEVEL_COUNT:
                state["level"] += 1
                state["recent_results"] = []
                level_change = 1
            elif (
                wrong_count >= 2 or slow_count >= 3
            ) and state["level"] > 1:
                state["level"] -= 1
                state["recent_results"] = []
                level_change = -1

    event = {
        "timing_class": timing_class,
        "is_suspicious": timing_class == "suspicious",
        "earned_bonus": earned_bonus,
        "bonus_target_seconds": target_seconds,
        "affects_progress": affects_progress,
        "level_change": level_change,
    }
    return progress, event


def _new_pool(conninfo: str) -> AsyncConnectionPool:
    return AsyncConnectionPool(
        conninfo=conninfo,
        min_size=0,
        max_size=5,
        max_idle=60,
        max_lifetime=600,
        check=AsyncConnectionPool.check_connection,
        open=False,
    )


async def _prepare_schema(
    pool: AsyncConnectionPool, statements: list[str]
) -> None:
    async with pool.connection(timeout=30) as connection:
        for statement in statements:
            await connection.execute(statement)


async def connect_database(_) -> None:
    global _progress_pool, _log_pool, _log_pool_is_shared

    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        logging.warning(
            "DATABASE_URL is not set; progress will be stored in memory"
        )
        return

    _progress_pool = _new_pool(database_url)
    await _progress_pool.open()
    await _prepare_schema(_progress_pool, MAIN_SCHEMA)

    log_database_url = os.getenv("LOG_DATABASE_URL")
    if log_database_url:
        _log_pool = _new_pool(log_database_url)
        await _log_pool.open()
        await _prepare_schema(_log_pool, LOG_SCHEMA)
        _log_pool_is_shared = False
        logging.info("Separate PostgreSQL answer log is ready")
    else:
        _log_pool = _progress_pool
        _log_pool_is_shared = True
        await _prepare_schema(_log_pool, LOG_SCHEMA)
        logging.warning(
            "LOG_DATABASE_URL is not set; answer logs use DATABASE_URL"
        )

    logging.info("PostgreSQL progress schema is ready")


async def close_database(_) -> None:
    global _progress_pool, _log_pool, _log_pool_is_shared

    if _log_pool is not None and not _log_pool_is_shared:
        await _log_pool.close()
    if _progress_pool is not None:
        await _progress_pool.close()

    _progress_pool = None
    _log_pool = None
    _log_pool_is_shared = False
    logging.info("PostgreSQL connection pools closed")


async def _upsert_user(connection, user: Any) -> None:
    await connection.execute(
        UPSERT_USER,
        (user.id, user.username, user.first_name, user.last_name),
    )


async def _ensure_subjects(connection, user_id: int) -> None:
    for subject in SUBJECTS:
        await connection.execute(
            """
            INSERT INTO user_subject_progress (telegram_user_id, subject)
            VALUES (%s, %s)
            ON CONFLICT (telegram_user_id, subject) DO NOTHING
            """,
            (user_id, subject),
        )


def _subject_from_row(row) -> dict[str, Any]:
    return {
        "level": int(row[1]),
        "calibration_correct": int(row[2]),
        "trusted_times_ms": list(row[3] or []),
        "recent_results": list(row[4] or []),
        "total_answers": int(row[5]),
        "correct_answers": int(row[6]),
    }


async def _load_subject_rows(connection, user_id: int) -> dict[str, Any]:
    cursor = await connection.execute(
        """
        SELECT
            subject,
            level,
            calibration_correct,
            trusted_times_ms,
            recent_results,
            total_answers,
            correct_answers
        FROM user_subject_progress
        WHERE telegram_user_id = %s
        """,
        (user_id,),
    )
    rows = await cursor.fetchall()
    return {
        "subjects": {row[0]: _subject_from_row(row) for row in rows}
    }


async def get_progress(user: Any) -> dict[str, Any]:
    if _progress_pool is None:
        return empty_progress()

    async with _progress_pool.connection() as connection:
        async with connection.transaction():
            await _upsert_user(connection, user)
            await _ensure_subjects(connection, user.id)
            progress = await _load_subject_rows(connection, user.id)

    return progress


async def _save_log(
    *,
    user: Any,
    subject: str,
    task_id: int,
    expression: str,
    selected_answer: str,
    correct_answer: str,
    is_correct: bool,
    first_attempt: bool,
    difficulty_level: int,
    answer_options: int,
    response_time_ms: int,
    event: dict[str, Any],
) -> None:
    if _log_pool is None:
        return

    async with _log_pool.connection() as connection:
        await connection.execute(
            """
            INSERT INTO log_users (
                telegram_user_id, username, first_name, last_name
            )
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (telegram_user_id) DO UPDATE SET
                username = EXCLUDED.username,
                first_name = EXCLUDED.first_name,
                last_name = EXCLUDED.last_name,
                updated_at = NOW()
            """,
            (user.id, user.username, user.first_name, user.last_name),
        )
        await connection.execute(
            """
            INSERT INTO answer_logs (
                telegram_user_id,
                subject,
                task_id,
                expression,
                selected_answer,
                correct_answer,
                is_correct,
                first_attempt,
                affects_progress,
                difficulty_level,
                answer_options,
                response_time_ms,
                timing_class,
                is_suspicious,
                earned_bonus,
                bonus_target_seconds
            )
            VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s
            )
            """,
            (
                user.id,
                subject,
                task_id,
                expression,
                selected_answer,
                correct_answer,
                is_correct,
                first_attempt,
                event["affects_progress"],
                difficulty_level,
                answer_options,
                response_time_ms,
                event["timing_class"],
                event["is_suspicious"],
                event["earned_bonus"],
                event["bonus_target_seconds"],
            ),
        )


async def record_answer(
    user: Any,
    task_id: int,
    expression: str,
    selected_answer: str,
    correct_answer: str,
    subject: str,
    difficulty_level: int,
    answer_options: int,
    response_time_ms: int,
    first_attempt: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if _progress_pool is None:
        raise RuntimeError("Progress database is not connected")

    is_correct = selected_answer == correct_answer

    async with _progress_pool.connection() as connection:
        async with connection.transaction():
            await _upsert_user(connection, user)
            await _ensure_subjects(connection, user.id)
            cursor = await connection.execute(
                """
                SELECT
                    subject,
                    level,
                    calibration_correct,
                    trusted_times_ms,
                    recent_results,
                    total_answers,
                    correct_answers
                FROM user_subject_progress
                WHERE telegram_user_id = %s AND subject = %s
                FOR UPDATE
                """,
                (user.id, subject),
            )
            row = await cursor.fetchone()
            progress = await _load_subject_rows(connection, user.id)
            progress["subjects"][subject] = _subject_from_row(row)
            progress, event = apply_answer_to_progress(
                progress,
                subject,
                is_correct,
                response_time_ms,
                first_attempt,
            )
            state = progress["subjects"][subject]

            await connection.execute(
                """
                UPDATE user_subject_progress
                SET
                    level = %s,
                    calibration_correct = %s,
                    trusted_times_ms = %s,
                    recent_results = %s,
                    total_answers = %s,
                    correct_answers = %s,
                    updated_at = NOW()
                WHERE telegram_user_id = %s AND subject = %s
                """,
                (
                    state["level"],
                    state["calibration_correct"],
                    state["trusted_times_ms"],
                    state["recent_results"],
                    state["total_answers"],
                    state["correct_answers"],
                    user.id,
                    subject,
                ),
            )

    try:
        await _save_log(
            user=user,
            subject=subject,
            task_id=task_id,
            expression=expression,
            selected_answer=selected_answer,
            correct_answer=correct_answer,
            is_correct=is_correct,
            first_attempt=first_attempt,
            difficulty_level=difficulty_level,
            answer_options=answer_options,
            response_time_ms=response_time_ms,
            event=event,
        )
    except Exception:
        logging.exception("Could not write answer event to the log database")

    return progress, event
