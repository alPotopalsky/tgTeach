import json
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
    """
    CREATE TABLE IF NOT EXISTS user_topic_cooldowns (
        telegram_user_id BIGINT NOT NULL
            REFERENCES bot_users (telegram_user_id) ON DELETE CASCADE,
        subject TEXT NOT NULL,
        topic TEXT NOT NULL,
        blocked_until TIMESTAMPTZ NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        PRIMARY KEY (telegram_user_id, subject, topic)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS user_topic_cooldowns_expiry_idx
    ON user_topic_cooldowns (blocked_until)
    """,
    """
    CREATE TABLE IF NOT EXISTS content_questions (
        question_id TEXT PRIMARY KEY,
        subject TEXT NOT NULL,
        topic TEXT NOT NULL,
        concept_id TEXT,
        difficulty_level SMALLINT NOT NULL
            CHECK (difficulty_level BETWEEN 1 AND 10),
        prompt TEXT NOT NULL,
        answer_options JSONB NOT NULL,
        correct_answer TEXT NOT NULL,
        explanation TEXT NOT NULL,
        is_entry BOOLEAN NOT NULL DEFAULT FALSE,
        active BOOLEAN NOT NULL DEFAULT TRUE,
        source TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    ALTER TABLE content_questions
    ADD COLUMN IF NOT EXISTS concept_id TEXT
    """,
    """
    CREATE TABLE IF NOT EXISTS question_edges (
        from_question_id TEXT NOT NULL
            REFERENCES content_questions (question_id) ON DELETE CASCADE,
        outcome TEXT NOT NULL
            CHECK (
                outcome IN (
                    'correct',
                    'wrong',
                    'prerequisite',
                    'related'
                )
            ),
        to_question_id TEXT NOT NULL
            REFERENCES content_questions (question_id) ON DELETE CASCADE,
        priority SMALLINT NOT NULL DEFAULT 1,
        PRIMARY KEY (
            from_question_id,
            outcome,
            to_question_id
        )
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS question_edges_route_idx
    ON question_edges (from_question_id, outcome, priority)
    """,
    """
    CREATE TABLE IF NOT EXISTS user_question_progress (
        telegram_user_id BIGINT NOT NULL
            REFERENCES bot_users (telegram_user_id) ON DELETE CASCADE,
        question_id TEXT NOT NULL
            REFERENCES content_questions (question_id) ON DELETE CASCADE,
        attempts INTEGER NOT NULL DEFAULT 0,
        correct_answers INTEGER NOT NULL DEFAULT 0,
        mastery_score SMALLINT NOT NULL DEFAULT 0
            CHECK (mastery_score BETWEEN 0 AND 100),
        last_answered_at TIMESTAMPTZ,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        PRIMARY KEY (telegram_user_id, question_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS user_session_profiles (
        telegram_user_id BIGINT PRIMARY KEY
            REFERENCES bot_users (telegram_user_id) ON DELETE CASCADE,
        focus_block_seconds SMALLINT NOT NULL DEFAULT 480
            CHECK (focus_block_seconds BETWEEN 300 AND 720),
        sessions_observed INTEGER NOT NULL DEFAULT 0,
        recent_outcomes TEXT[] NOT NULL DEFAULT '{}',
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS learning_sessions (
        id BIGSERIAL PRIMARY KEY,
        telegram_user_id BIGINT NOT NULL
            REFERENCES bot_users (telegram_user_id) ON DELETE CASCADE,
        started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        last_activity_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        active_seconds INTEGER NOT NULL DEFAULT 0,
        focus_block_seconds SMALLINT NOT NULL,
        completed_blocks SMALLINT NOT NULL DEFAULT 0,
        level_advances SMALLINT NOT NULL DEFAULT 0,
        ended_at TIMESTAMPTZ,
        end_reason TEXT
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS learning_sessions_one_open_idx
    ON learning_sessions (telegram_user_id)
    WHERE ended_at IS NULL
    """,
    """
    CREATE INDEX IF NOT EXISTS learning_sessions_user_time_idx
    ON learning_sessions (telegram_user_id, started_at DESC)
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
    """
    CREATE TABLE IF NOT EXISTS topic_feedback_logs (
        id BIGSERIAL PRIMARY KEY,
        telegram_user_id BIGINT NOT NULL
            REFERENCES log_users (telegram_user_id) ON DELETE CASCADE,
        subject TEXT NOT NULL,
        topic TEXT NOT NULL,
        expression TEXT NOT NULL,
        action TEXT NOT NULL,
        blocked_until TIMESTAMPTZ NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS topic_feedback_logs_user_time_idx
    ON topic_feedback_logs (telegram_user_id, created_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS topic_feedback_logs_created_at_idx
    ON topic_feedback_logs (created_at)
    """,
    """
    CREATE TABLE IF NOT EXISTS content_answer_logs (
        id BIGSERIAL PRIMARY KEY,
        telegram_user_id BIGINT NOT NULL
            REFERENCES log_users (telegram_user_id) ON DELETE CASCADE,
        question_id TEXT NOT NULL,
        selected_answer TEXT NOT NULL,
        correct_answer TEXT NOT NULL,
        is_correct BOOLEAN NOT NULL,
        response_time_ms INTEGER NOT NULL,
        answered_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS content_answer_logs_user_time_idx
    ON content_answer_logs (telegram_user_id, answered_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS content_answer_logs_question_idx
    ON content_answer_logs (question_id, answered_at DESC)
    """,
]

CONTENT_QUESTIONS = [
    {
        "question_id": "health_defibrillator_action",
        "subject": "curiosity",
        "topic": "heart_and_first_aid",
        "concept_id": "aed_operation",
        "difficulty_level": 5,
        "prompt": (
            "Що робить автоматичний дефібрилятор (AED) перед тим, "
            "як допомогти?"
        ),
        "answer_options": [
            (
                "Перевіряє ритм серця і визначає, чи потрібен "
                "електричний імпульс"
            ),
            "Одразу подає імпульс, не перевіряючи ритм",
            "Лише рахує кількість ударів серця",
        ],
        "correct_answer": (
            "Перевіряє ритм серця і визначає, чи потрібен "
            "електричний імпульс"
        ),
        "explanation": (
            "AED спочатку аналізує ритм і пропонує імпульс лише "
            "тоді, коли він справді може допомогти."
        ),
        "is_entry": True,
        "source": (
            "https://www.heart.org/en/news/2023/01/17/"
            "5-things-to-know-about-aeds-after-a-defibrillator-"
            "helped-save-damar-hamlin"
        ),
    },
    {
        "question_id": "health_heart_rhythm",
        "subject": "curiosity",
        "topic": "heart_and_first_aid",
        "concept_id": "heart_rhythm",
        "difficulty_level": 3,
        "prompt": "Що означає «перевірити ритм серця»?",
        "answer_options": [
            "Послідовність і частоту скорочень серця",
            "Кількість крові в організмі",
            "Температуру серцевого м’яза",
        ],
        "correct_answer": "Послідовність і частоту скорочень серця",
        "explanation": (
            "Ритм описує, як регулярно і з якою частотою серце "
            "скорочується."
        ),
        "is_entry": False,
        "source": "https://www.heart.org/en/health-topics/arrhythmia",
    },
    {
        "question_id": "health_rhythm_pattern",
        "subject": "curiosity",
        "topic": "heart_and_first_aid",
        "concept_id": "heart_rhythm",
        "difficulty_level": 4,
        "prompt": "Який опис найбільше схожий на рівний ритм?",
        "answer_options": [
            "Удари відбуваються через приблизно однакові проміжки",
            "Кожен наступний удар обов’язково сильніший",
            "Серце час від часу повністю змінює напрямок роботи",
        ],
        "correct_answer": (
            "Удари відбуваються через приблизно однакові проміжки"
        ),
        "explanation": (
            "Рівний ритм означає, що скорочення повторюються "
            "послідовно через схожі проміжки часу."
        ),
        "is_entry": False,
        "source": "https://www.heart.org/en/health-topics/arrhythmia",
    },
    {
        "question_id": "health_heart_job",
        "subject": "curiosity",
        "topic": "heart_and_first_aid",
        "concept_id": "heart_as_pump",
        "difficulty_level": 1,
        "prompt": "Яка головна робота серця?",
        "answer_options": [
            "Перекачувати кров організмом",
            "Допомагати легеням вдихати повітря",
            "Перетравлювати їжу",
        ],
        "correct_answer": "Перекачувати кров організмом",
        "explanation": (
            "Серце працює як насос і рухає кров, яка переносить "
            "кисень та поживні речовини."
        ),
        "is_entry": False,
        "source": (
            "https://www.nhlbi.nih.gov/health/heart/"
            "how-the-heart-works"
        ),
    },
    {
        "question_id": "health_blood_delivery",
        "subject": "curiosity",
        "topic": "heart_and_first_aid",
        "concept_id": "heart_as_pump",
        "difficulty_level": 2,
        "prompt": "Навіщо серце постійно перекачує кров?",
        "answer_options": [
            "Щоб доставляти клітинам кисень і поживні речовини",
            "Щоб підтримувати однакову довжину судин",
            "Щоб легені могли самі рухатися",
        ],
        "correct_answer": (
            "Щоб доставляти клітинам кисень і поживні речовини"
        ),
        "explanation": (
            "Кров переносить потрібні клітинам речовини, а серце "
            "підтримує її рух організмом."
        ),
        "is_entry": False,
        "source": (
            "https://www.nhlbi.nih.gov/health/heart/"
            "how-the-heart-works"
        ),
    },
    {
        "question_id": "health_aed_analysis",
        "subject": "curiosity",
        "topic": "heart_and_first_aid",
        "concept_id": "aed_operation",
        "difficulty_level": 7,
        "prompt": (
            "Чому AED не подає електричний імпульс щоразу?"
        ),
        "answer_options": [
            "Імпульс допомагає лише за певних порушень ритму",
            "Пристрій намагається зберегти заряд батареї",
            "Спочатку він має отримати дозвіл лікаря",
        ],
        "correct_answer": (
            "Імпульс допомагає лише за певних порушень ритму"
        ),
        "explanation": (
            "AED розрізняє ритми й не пропонує імпульс, якщо той "
            "не допоможе."
        ),
        "is_entry": False,
        "source": (
            "https://www.redcross.org/take-a-class/"
            "organizations/aed-program"
        ),
    },
    {
        "question_id": "health_flatline",
        "subject": "curiosity",
        "topic": "heart_and_first_aid",
        "concept_id": "aed_operation",
        "difficulty_level": 8,
        "prompt": (
            "Як AED допомагає людині, яка не є медиком?"
        ),
        "answer_options": [
            "Дає голосові підказки й сам аналізує ритм",
            "Працює лише після дистанційного дозволу лікаря",
            "Лише показує на екрані число ударів серця",
        ],
        "correct_answer": (
            "Дає голосові підказки й сам аналізує ритм"
        ),
        "explanation": (
            "Автоматичний пристрій веде користувача крок за кроком "
            "і сам вирішує, чи пропонувати імпульс."
        ),
        "is_entry": False,
        "source": (
            "https://www.redcross.org/take-a-class/"
            "organizations/aed-program"
        ),
    },
]

CONTENT_EDGES = [
    ("health_defibrillator_action", "correct", "health_aed_analysis", 1),
    ("health_defibrillator_action", "wrong", "health_heart_rhythm", 1),
    ("health_heart_rhythm", "correct", "health_rhythm_pattern", 1),
    ("health_heart_rhythm", "wrong", "health_heart_job", 1),
    ("health_rhythm_pattern", "wrong", "health_heart_rhythm", 1),
    ("health_heart_job", "correct", "health_blood_delivery", 1),
    ("health_blood_delivery", "wrong", "health_heart_job", 1),
    ("health_aed_analysis", "correct", "health_flatline", 1),
    ("health_aed_analysis", "wrong", "health_defibrillator_action", 1),
    ("health_flatline", "wrong", "health_aed_analysis", 1),
]

REMOVED_CONTENT_EDGES = [
    ("health_heart_rhythm", "correct", "health_defibrillator_action"),
    ("health_heart_job", "correct", "health_heart_rhythm"),
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
        },
        "blocked_topics": {},
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
    offered_bonus_target_seconds: int | None = None,
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
    target_seconds = (
        offered_bonus_target_seconds
        if offered_bonus_target_seconds is not None
        else bonus_target_seconds(state)
    )
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


async def _seed_content(pool: AsyncConnectionPool) -> None:
    async with pool.connection(timeout=30) as connection:
        async with connection.transaction():
            for question in CONTENT_QUESTIONS:
                await connection.execute(
                    """
                    INSERT INTO content_questions (
                        question_id,
                        subject,
                        topic,
                        difficulty_level,
                        prompt,
                        answer_options,
                        correct_answer,
                        explanation,
                        is_entry,
                        concept_id,
                        source
                    )
                    VALUES (
                        %s, %s, %s, %s, %s, %s::jsonb,
                        %s, %s, %s, %s, %s
                    )
                    ON CONFLICT (question_id) DO UPDATE SET
                        subject = EXCLUDED.subject,
                        topic = EXCLUDED.topic,
                        difficulty_level = EXCLUDED.difficulty_level,
                        prompt = EXCLUDED.prompt,
                        answer_options = EXCLUDED.answer_options,
                        correct_answer = EXCLUDED.correct_answer,
                        explanation = EXCLUDED.explanation,
                        is_entry = EXCLUDED.is_entry,
                        concept_id = EXCLUDED.concept_id,
                        active = TRUE,
                        source = EXCLUDED.source,
                        updated_at = NOW()
                    """,
                    (
                        question["question_id"],
                        question["subject"],
                        question["topic"],
                        question["difficulty_level"],
                        question["prompt"],
                        json.dumps(
                            question["answer_options"],
                            ensure_ascii=False,
                        ),
                        question["correct_answer"],
                        question["explanation"],
                        question["is_entry"],
                        question["concept_id"],
                        question["source"],
                    ),
                )

            for edge in REMOVED_CONTENT_EDGES:
                await connection.execute(
                    """
                    DELETE FROM question_edges
                    WHERE from_question_id = %s
                      AND outcome = %s
                      AND to_question_id = %s
                    """,
                    edge,
                )

            for edge in CONTENT_EDGES:
                await connection.execute(
                    """
                    INSERT INTO question_edges (
                        from_question_id,
                        outcome,
                        to_question_id,
                        priority
                    )
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (
                        from_question_id,
                        outcome,
                        to_question_id
                    ) DO UPDATE SET
                        priority = EXCLUDED.priority
                    """,
                    edge,
                )


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
    await _seed_content(_progress_pool)

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
    cooldown_cursor = await connection.execute(
        """
        SELECT
            subject || ':' || topic,
            EXTRACT(EPOCH FROM blocked_until)
        FROM user_topic_cooldowns
        WHERE telegram_user_id = %s
          AND blocked_until > NOW()
        """,
        (user_id,),
    )
    cooldown_rows = await cooldown_cursor.fetchall()
    return {
        "subjects": {row[0]: _subject_from_row(row) for row in rows},
        "blocked_topics": {
            row[0]: float(row[1]) for row in cooldown_rows
        },
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


def _fallback_content_question(
    question_id: str | None = None,
) -> dict[str, Any] | None:
    candidates = [
        question
        for question in CONTENT_QUESTIONS
        if (
            question_id is None
            and question["is_entry"]
        )
        or question["question_id"] == question_id
    ]
    if not candidates:
        return None
    return {
        **candidates[0],
        "answer_options": list(candidates[0]["answer_options"]),
    }


def _content_question_from_row(row) -> dict[str, Any]:
    return {
        "question_id": row[0],
        "subject": row[1],
        "topic": row[2],
        "difficulty_level": int(row[3]),
        "prompt": row[4],
        "answer_options": list(row[5]),
        "correct_answer": row[6],
        "explanation": row[7],
        "is_entry": bool(row[8]),
        "concept_id": row[9],
    }


async def get_content_question(
    question_id: str | None = None,
    user_id: int | None = None,
) -> dict[str, Any] | None:
    if _progress_pool is None:
        return _fallback_content_question(question_id)

    async with _progress_pool.connection() as connection:
        if question_id is not None:
            cursor = await connection.execute(
                """
                SELECT
                    question_id,
                    subject,
                    topic,
                    difficulty_level,
                    prompt,
                    answer_options,
                    correct_answer,
                    explanation,
                    is_entry,
                    concept_id
                FROM content_questions
                WHERE question_id = %s AND active
                """,
                (question_id,),
            )
        else:
            cursor = await connection.execute(
                """
                SELECT
                    q.question_id,
                    q.subject,
                    q.topic,
                    q.difficulty_level,
                    q.prompt,
                    q.answer_options,
                    q.correct_answer,
                    q.explanation,
                    q.is_entry,
                    q.concept_id
                FROM content_questions AS q
                LEFT JOIN user_question_progress AS p
                    ON p.question_id = q.question_id
                   AND p.telegram_user_id = %s
                WHERE q.active AND q.is_entry
                ORDER BY
                    p.last_answered_at ASC NULLS FIRST,
                    RANDOM()
                LIMIT 1
                """,
                (user_id,),
            )
        row = await cursor.fetchone()

    return _content_question_from_row(row) if row else None


async def get_next_content_question(
    question_id: str,
    is_correct: bool,
) -> dict[str, Any] | None:
    outcome = "correct" if is_correct else "wrong"

    if _progress_pool is None:
        candidates = [
            edge
            for edge in CONTENT_EDGES
            if edge[0] == question_id and edge[1] == outcome
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda edge: edge[3])
        next_question = _fallback_content_question(candidates[0][2])
        current_question = _fallback_content_question(question_id)
        if (
            outcome == "correct"
            and next_question is not None
            and current_question is not None
            and next_question["concept_id"]
            != current_question["concept_id"]
        ):
            return None
        return next_question

    async with _progress_pool.connection() as connection:
        cursor = await connection.execute(
            """
            SELECT q.question_id
            FROM question_edges AS e
            JOIN content_questions AS q
              ON q.question_id = e.to_question_id
            JOIN content_questions AS source_q
              ON source_q.question_id = e.from_question_id
            WHERE e.from_question_id = %s
              AND e.outcome = %s
              AND q.active
              AND (
                    e.outcome <> 'correct'
                    OR (
                        source_q.concept_id IS NOT NULL
                        AND q.concept_id = source_q.concept_id
                    )
              )
            ORDER BY e.priority, RANDOM()
            LIMIT 1
            """,
            (question_id, outcome),
        )
        row = await cursor.fetchone()

    return await get_content_question(row[0]) if row else None


async def record_content_answer(
    *,
    user: Any,
    question_id: str,
    selected_answer: str,
    correct_answer: str,
    response_time_ms: int,
) -> None:
    is_correct = selected_answer == correct_answer

    if _progress_pool is not None:
        async with _progress_pool.connection() as connection:
            async with connection.transaction():
                await _upsert_user(connection, user)
                await connection.execute(
                    """
                    INSERT INTO user_question_progress (
                        telegram_user_id,
                        question_id,
                        attempts,
                        correct_answers,
                        mastery_score,
                        last_answered_at
                    )
                    VALUES (
                        %s,
                        %s,
                        1,
                        %s,
                        %s,
                        NOW()
                    )
                    ON CONFLICT (telegram_user_id, question_id)
                    DO UPDATE SET
                        attempts =
                            user_question_progress.attempts + 1,
                        correct_answers =
                            user_question_progress.correct_answers
                            + EXCLUDED.correct_answers,
                        mastery_score = LEAST(
                            100,
                            GREATEST(
                                0,
                                user_question_progress.mastery_score
                                + %s
                            )
                        ),
                        last_answered_at = NOW(),
                        updated_at = NOW()
                    """,
                    (
                        user.id,
                        question_id,
                        int(is_correct),
                        15 if is_correct else 0,
                        15 if is_correct else -5,
                    ),
                )

    if _log_pool is not None:
        try:
            async with _log_pool.connection() as connection:
                async with connection.transaction():
                    await connection.execute(
                        """
                        INSERT INTO log_users (
                            telegram_user_id,
                            username,
                            first_name,
                            last_name
                        )
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT (telegram_user_id) DO UPDATE SET
                            username = EXCLUDED.username,
                            first_name = EXCLUDED.first_name,
                            last_name = EXCLUDED.last_name,
                            updated_at = NOW()
                        """,
                        (
                            user.id,
                            user.username,
                            user.first_name,
                            user.last_name,
                        ),
                    )
                    await connection.execute(
                        """
                        INSERT INTO content_answer_logs (
                            telegram_user_id,
                            question_id,
                            selected_answer,
                            correct_answer,
                            is_correct,
                            response_time_ms
                        )
                        VALUES (%s, %s, %s, %s, %s, %s)
                        """,
                        (
                            user.id,
                            question_id,
                            selected_answer,
                            correct_answer,
                            is_correct,
                            response_time_ms,
                        ),
                    )
        except Exception:
            logging.exception("Could not save content answer log")


async def record_session_activity(
    *,
    user: Any,
    active_seconds: int,
    level_advance: bool = False,
) -> dict[str, Any] | None:
    if _progress_pool is None:
        return None

    active_seconds = max(0, min(20, int(active_seconds)))
    async with _progress_pool.connection() as connection:
        async with connection.transaction():
            await _upsert_user(connection, user)
            await connection.execute(
                """
                INSERT INTO user_session_profiles (telegram_user_id)
                VALUES (%s)
                ON CONFLICT (telegram_user_id) DO NOTHING
                """,
                (user.id,),
            )
            cursor = await connection.execute(
                """
                SELECT
                    id,
                    active_seconds,
                    focus_block_seconds,
                    completed_blocks,
                    level_advances,
                    last_activity_at < NOW() - INTERVAL '10 minutes'
                FROM learning_sessions
                WHERE telegram_user_id = %s
                  AND ended_at IS NULL
                FOR UPDATE
                """,
                (user.id,),
            )
            row = await cursor.fetchone()

            if row is not None and row[5]:
                await connection.execute(
                    """
                    UPDATE learning_sessions
                    SET ended_at = NOW(), end_reason = 'inactivity'
                    WHERE id = %s
                    """,
                    (row[0],),
                )
                row = None

            if row is None:
                profile_cursor = await connection.execute(
                    """
                    SELECT focus_block_seconds
                    FROM user_session_profiles
                    WHERE telegram_user_id = %s
                    """,
                    (user.id,),
                )
                block_seconds = int((await profile_cursor.fetchone())[0])
                cursor = await connection.execute(
                    """
                    INSERT INTO learning_sessions (
                        telegram_user_id,
                        focus_block_seconds
                    )
                    VALUES (%s, %s)
                    RETURNING
                        id,
                        active_seconds,
                        focus_block_seconds,
                        completed_blocks,
                        level_advances,
                        FALSE
                    """,
                    (user.id, block_seconds),
                )
                row = await cursor.fetchone()

            (
                session_id,
                previous_active_seconds,
                block_seconds,
                previous_completed_blocks,
                previous_level_advances,
                _,
            ) = row
            new_active_seconds = int(previous_active_seconds) + active_seconds
            new_completed_blocks = new_active_seconds // int(block_seconds)
            new_level_advances = int(previous_level_advances) + int(
                level_advance
            )
            await connection.execute(
                """
                UPDATE learning_sessions
                SET
                    active_seconds = %s,
                    completed_blocks = %s,
                    level_advances = %s,
                    last_activity_at = NOW()
                WHERE id = %s
                """,
                (
                    new_active_seconds,
                    new_completed_blocks,
                    new_level_advances,
                    session_id,
                ),
            )

    return {
        "session_id": int(session_id),
        "active_seconds": new_active_seconds,
        "focus_block_seconds": int(block_seconds),
        "completed_blocks": int(new_completed_blocks),
        "new_block_completed": (
            new_completed_blocks > int(previous_completed_blocks)
        ),
        "level_advances": new_level_advances,
    }


async def end_learning_session(
    user_id: int,
    reason: str,
) -> dict[str, Any] | None:
    if _progress_pool is None:
        return None

    async with _progress_pool.connection() as connection:
        async with connection.transaction():
            cursor = await connection.execute(
                """
                SELECT
                    id,
                    active_seconds,
                    focus_block_seconds,
                    completed_blocks,
                    level_advances
                FROM learning_sessions
                WHERE telegram_user_id = %s
                  AND ended_at IS NULL
                FOR UPDATE
                """,
                (user_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                return None

            (
                session_id,
                active_seconds,
                block_seconds,
                completed_blocks,
                level_advances,
            ) = row
            await connection.execute(
                """
                UPDATE learning_sessions
                SET ended_at = NOW(), end_reason = %s
                WHERE id = %s
                """,
                (reason, session_id),
            )

            profile_cursor = await connection.execute(
                """
                SELECT recent_outcomes
                FROM user_session_profiles
                WHERE telegram_user_id = %s
                FOR UPDATE
                """,
                (user_id,),
            )
            profile_row = await profile_cursor.fetchone()
            recent_outcomes = list(profile_row[0] or []) if profile_row else []
            if int(completed_blocks) >= 3:
                outcome = "stable"
            elif int(active_seconds) < int(block_seconds):
                outcome = "early"
            else:
                outcome = "partial"
            recent_outcomes = (recent_outcomes + [outcome])[-3:]

            new_block_seconds = int(block_seconds)
            if recent_outcomes[-2:] == ["early", "early"]:
                new_block_seconds = max(300, new_block_seconds - 60)
                recent_outcomes = []
            elif recent_outcomes[-2:] == ["stable", "stable"]:
                new_block_seconds = min(720, new_block_seconds + 60)
                recent_outcomes = []

            await connection.execute(
                """
                UPDATE user_session_profiles
                SET
                    focus_block_seconds = %s,
                    sessions_observed = sessions_observed + 1,
                    recent_outcomes = %s,
                    updated_at = NOW()
                WHERE telegram_user_id = %s
                """,
                (new_block_seconds, recent_outcomes, user_id),
            )

    return {
        "session_id": int(session_id),
        "active_seconds": int(active_seconds),
        "focus_block_seconds": int(block_seconds),
        "completed_blocks": int(completed_blocks),
        "level_advances": int(level_advances),
        "next_focus_block_seconds": new_block_seconds,
    }


async def get_learning_summary(user_id: int) -> dict[str, Any] | None:
    if _progress_pool is None:
        return None

    async with _progress_pool.connection() as connection:
        profile_cursor = await connection.execute(
            """
            SELECT focus_block_seconds
            FROM user_session_profiles
            WHERE telegram_user_id = %s
            """,
            (user_id,),
        )
        profile_row = await profile_cursor.fetchone()
        session_cursor = await connection.execute(
            """
            SELECT
                active_seconds,
                focus_block_seconds,
                completed_blocks,
                level_advances
            FROM learning_sessions
            WHERE telegram_user_id = %s
              AND ended_at IS NULL
            """,
            (user_id,),
        )
        session_row = await session_cursor.fetchone()

    block_seconds = int(profile_row[0]) if profile_row else 480
    if session_row is None:
        return {
            "active_seconds": 0,
            "focus_block_seconds": block_seconds,
            "completed_blocks": 0,
            "level_advances": 0,
        }
    return {
        "active_seconds": int(session_row[0]),
        "focus_block_seconds": int(session_row[1]),
        "completed_blocks": int(session_row[2]),
        "level_advances": int(session_row[3]),
    }


async def block_topic(
    *,
    user: Any,
    subject: str,
    topic: str,
    expression: str,
    hours: int = 24,
) -> float:
    if _progress_pool is None:
        raise RuntimeError("Progress database is not connected")

    async with _progress_pool.connection() as connection:
        async with connection.transaction():
            await _upsert_user(connection, user)
            cursor = await connection.execute(
                """
                INSERT INTO user_topic_cooldowns (
                    telegram_user_id,
                    subject,
                    topic,
                    blocked_until
                )
                VALUES (
                    %s,
                    %s,
                    %s,
                    NOW() + (%s * INTERVAL '1 hour')
                )
                ON CONFLICT (telegram_user_id, subject, topic)
                DO UPDATE SET
                    blocked_until = EXCLUDED.blocked_until,
                    updated_at = NOW()
                RETURNING
                    blocked_until,
                    EXTRACT(EPOCH FROM blocked_until)
                """,
                (user.id, subject, topic, hours),
            )
            row = await cursor.fetchone()

    if _log_pool is not None:
        try:
            async with _log_pool.connection() as connection:
                async with connection.transaction():
                    await connection.execute(
                        """
                        INSERT INTO log_users (
                            telegram_user_id,
                            username,
                            first_name,
                            last_name
                        )
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT (telegram_user_id) DO UPDATE SET
                            username = EXCLUDED.username,
                            first_name = EXCLUDED.first_name,
                            last_name = EXCLUDED.last_name,
                            updated_at = NOW()
                        """,
                        (
                            user.id,
                            user.username,
                            user.first_name,
                            user.last_name,
                        ),
                    )
                    await connection.execute(
                        """
                        INSERT INTO topic_feedback_logs (
                            telegram_user_id,
                            subject,
                            topic,
                            expression,
                            action,
                            blocked_until
                        )
                        VALUES (%s, %s, %s, %s, 'bored', %s)
                        """,
                        (
                            user.id,
                            subject,
                            topic,
                            expression,
                            row[0],
                        ),
                    )
        except Exception:
            logging.exception("Could not save topic feedback log")

    return float(row[1])


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
    offered_bonus_target_seconds: int | None = None,
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
                offered_bonus_target_seconds,
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
