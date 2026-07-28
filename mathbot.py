import asyncio
import hashlib
import logging
import os
import random
from pathlib import Path
from time import monotonic

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import database


def load_bot_token() -> str:
    """Load the Telegram bot token from the environment or a local file."""
    token = os.getenv("BOT_TOKEN")
    if token:
        return token.strip()

    token_path = Path(__file__).with_name("token.txt")
    if token_path.exists():
        return token_path.read_text(encoding="utf-8").strip()

    raise RuntimeError(
        "Telegram bot token is missing. Set BOT_TOKEN or create token.txt."
    )


logging.basicConfig(level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

CORRECT_EMOJIS = ["🎉", "🥳", "🤩", "🏆", "🚀", "🌟"]
WRONG_EMOJIS = ["😅", "🙈", "🤔", "🫠", "🥲", "🧐"]

MATH_LEVEL_COUNT = database.LEVEL_COUNT
ENGLISH_LEVEL_COUNT = database.LEVEL_COUNT

ENGLISH_WORDS = [
    (0, "cat", "кіт"),
    (0, "dog", "собака"),
    (0, "sun", "сонце"),
    (0, "book", "книга"),
    (0, "apple", "яблуко"),
    (0, "water", "вода"),
    (0, "house", "будинок"),
    (0, "tree", "дерево"),
    (1, "school", "школа"),
    (1, "window", "вікно"),
    (1, "garden", "сад"),
    (1, "morning", "ранок"),
    (1, "family", "родина"),
    (1, "picture", "малюнок"),
    (1, "chair", "стілець"),
    (1, "bread", "хліб"),
    (2, "red", "червоний"),
    (2, "blue", "синій"),
    (2, "green", "зелений"),
    (2, "yellow", "жовтий"),
    (2, "white", "білий"),
    (2, "black", "чорний"),
    (3, "hand", "рука"),
    (3, "head", "голова"),
    (3, "eye", "око"),
    (3, "milk", "молоко"),
    (3, "cheese", "сир"),
    (3, "soup", "суп"),
    (4, "river", "річка"),
    (4, "forest", "ліс"),
    (4, "rain", "дощ"),
    (4, "snow", "сніг"),
    (4, "day", "день"),
    (4, "night", "ніч"),
    (5, "run", "бігти"),
    (5, "read", "читати"),
    (5, "write", "писати"),
    (5, "speak", "говорити"),
    (5, "listen", "слухати"),
    (5, "play", "грати"),
    (6, "big", "великий"),
    (6, "small", "маленький"),
    (6, "fast", "швидкий"),
    (6, "slow", "повільний"),
    (6, "warm", "теплий"),
    (6, "cold", "холодний"),
    (7, "street", "вулиця"),
    (7, "city", "місто"),
    (7, "bus", "автобус"),
    (7, "train", "потяг"),
    (7, "ticket", "квиток"),
    (7, "journey", "подорож"),
    (8, "question", "питання"),
    (8, "answer", "відповідь"),
    (8, "choice", "вибір"),
    (8, "language", "мова"),
    (8, "knowledge", "знання"),
    (8, "success", "успіх"),
    (9, "future", "майбутнє"),
    (9, "courage", "сміливість"),
    (9, "memory", "пам’ять"),
    (9, "freedom", "свобода"),
    (9, "health", "здоров’я"),
    (9, "science", "наука"),
]

MATH_TOPIC_BY_MODE = {
    "add_tiny": "addition",
    "add1": "addition",
    "add2_small": "addition",
    "add2": "addition",
    "add3": "addition",
    "add_large": "addition",
    "sub1": "subtraction",
    "sub2_small": "subtraction",
    "sub2": "subtraction",
    "sub3": "subtraction",
    "sub_large": "subtraction",
    "mul_tiny": "multiplication",
    "mul_easy": "multiplication",
    "mul": "multiplication",
    "mul_large": "multiplication",
    "div_easy": "division",
    "div_large": "division",
}
ENGLISH_WORDS_TOPIC = "basic_words"
INTERESTING_ROUTE_MAX_STEPS = 6
INTERESTING_BUTTON_TEXT = "✨ Щось цікаве"
INTERESTING_OFFER_MIN_SECONDS = 15
INTERESTING_OFFER_MAX_SECONDS = 30
INTERESTING_OFFER_WRONG_STEP_SECONDS = 3
MIN_BUBBLE_FILLER = "\u2800" * 18
WARMUP_RESET_SECONDS = 10 * 60
WARMUP_INITIAL_TARGET_SECONDS = 15
WARMUP_MIDDLE_TARGET_SECONDS = 10
WARMUP_MIDDLE_POINTS = 2
WARMUP_COMPLETE_POINTS = 4


def math_modes_for_level(level: int) -> list[str]:
    if level == 0:
        return ["add_tiny"]
    if level == 1:
        return ["add1", "sub1"]
    if level == 2:
        return ["add2_small", "sub2_small"]
    if level == 3:
        return ["add2_small", "sub2_small", "mul_tiny"]
    if level == 4:
        return ["add2", "sub2", "mul_easy"]
    if level == 5:
        return ["add2", "sub2", "mul"]
    if level == 6:
        return ["add2", "sub2", "mul", "div_easy"]
    if level == 7:
        return ["add3", "sub3", "mul"]
    if level == 8:
        return ["add3", "sub3", "mul_large", "div_large"]
    return ["add_large", "sub_large", "mul_large", "div_large"]


def generate_task(
    level: int = 0,
    available_modes: list[str] | None = None,
) -> tuple[str, int, str]:
    modes = available_modes or math_modes_for_level(level)
    mode = random.choice(modes)

    if mode == "add_tiny":
        a, b = random.randint(1, 5), random.randint(1, 5)
        operator = "+"
        answer = a + b
    elif mode == "add1":
        a, b = random.randint(2, 10), random.randint(2, 10)
        operator = "+"
        answer = a + b
    elif mode == "sub1":
        a, b = sorted(
            [random.randint(2, 15), random.randint(2, 15)], reverse=True
        )
        operator = "-"
        answer = a - b
    elif mode == "add2_small":
        a, b = random.randint(10, 49), random.randint(10, 49)
        operator = "+"
        answer = a + b
    elif mode == "sub2_small":
        a, b = sorted(
            [random.randint(10, 49), random.randint(10, 49)], reverse=True
        )
        operator = "-"
        answer = a - b
    elif mode == "mul_easy":
        a, b = random.randint(2, 10), random.randint(2, 10)
        operator = "×"
        answer = a * b
    elif mode == "mul_tiny":
        a, b = random.randint(2, 5), random.randint(2, 5)
        operator = "×"
        answer = a * b
    elif mode == "add2":
        a, b = random.randint(10, 99), random.randint(10, 99)
        operator = "+"
        answer = a + b
    elif mode == "sub2":
        a, b = sorted(
            [random.randint(10, 99), random.randint(10, 99)], reverse=True
        )
        operator = "-"
        answer = a - b
    elif mode == "add3":
        a, b = random.randint(100, 299), random.randint(100, 299)
        operator = "+"
        answer = a + b
    elif mode == "sub3":
        a, b = sorted(
            [random.randint(100, 299), random.randint(100, 299)], reverse=True
        )
        operator = "-"
        answer = a - b
    elif mode == "add_large":
        a, b = random.randint(300, 999), random.randint(300, 999)
        operator = "+"
        answer = a + b
    elif mode == "sub_large":
        a, b = sorted(
            [random.randint(300, 999), random.randint(300, 999)],
            reverse=True,
        )
        operator = "-"
        answer = a - b
    elif mode == "mul_large":
        a, b = random.randint(10, 20), random.randint(10, 20)
        operator = "×"
        answer = a * b
    elif mode == "div_easy":
        divisor = random.randint(2, 10)
        answer = random.randint(2, 10)
        a, b = divisor * answer, divisor
        operator = "÷"
    elif mode == "div_large":
        divisor = random.randint(10, 20)
        answer = random.randint(2, 15)
        a, b = divisor * answer, divisor
        operator = "÷"
    else:
        a, b = random.randint(2, 15), random.randint(2, 15)
        operator = "×"
        answer = a * b

    return f"{a} {operator} {b}", answer, MATH_TOPIC_BY_MODE[mode]


def generate_choices(answer: int, count: int) -> list[int]:
    choices = {answer}
    spread = max(count * 2, min(30, abs(answer) // 5 + 3))

    while len(choices) < count:
        offset = random.randint(1, spread)
        candidate = answer + random.choice([-1, 1]) * offset
        if candidate >= 0:
            choices.add(candidate)

    result = list(choices)
    random.shuffle(result)
    return result


def generate_english_task(
    level: int, choice_count: int
) -> tuple[str, str, list[str]]:
    available_words = [word for word in ENGLISH_WORDS if word[0] == level]
    _, english_word, correct_translation = random.choice(available_words)
    distractors = [
        translation
        for _, _, translation in available_words
        if translation != correct_translation
    ]
    choices = random.sample(distractors, choice_count - 1)
    choices.append(correct_translation)
    random.shuffle(choices)

    return (
        f"Що означає слово «{english_word}»?",
        correct_translation,
        choices,
    )


def answer_keyboard(
    choices: list[int | str],
    task_id: int,
) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(
            str(choice), callback_data=f"answer:{task_id}:{choice}"
        )
        for choice in choices
    ]
    row_size = 2 if len(buttons) == 4 else 3
    rows = [
        buttons[index : index + row_size]
        for index in range(0, len(buttons), row_size)
    ]
    return InlineKeyboardMarkup(rows)


def widen_short_message(text: str) -> str:
    if max(len(line) for line in text.splitlines()) >= 18:
        return text
    return f"{text}\n{MIN_BUBBLE_FILLER}"


def adaptive_bonus_target(
    context: ContextTypes.DEFAULT_TYPE,
    subject_progress: dict,
) -> int:
    personal_target = database.bonus_target_seconds(subject_progress)
    warmup_points = context.user_data.get("warmup_points", 0)

    if warmup_points < WARMUP_MIDDLE_POINTS:
        return WARMUP_INITIAL_TARGET_SECONDS
    if warmup_points < WARMUP_COMPLETE_POINTS:
        return max(
            WARMUP_MIDDLE_TARGET_SECONDS,
            personal_target or WARMUP_MIDDLE_TARGET_SECONDS,
        )
    return personal_target or WARMUP_MIDDLE_TARGET_SECONDS


def interesting_offer_delay(
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    expected_seconds = context.user_data.get(
        "task_bonus_target_seconds",
        WARMUP_INITIAL_TARGET_SECONDS,
    )
    wrong_attempts = min(
        context.user_data.get("task_attempts", 0),
        3,
    )
    predicted_delay = (
        expected_seconds * 2
        - wrong_attempts * INTERESTING_OFFER_WRONG_STEP_SECONDS
    )
    return max(
        INTERESTING_OFFER_MIN_SECONDS,
        min(INTERESTING_OFFER_MAX_SECONDS, round(predicted_delay)),
    )


def cancel_interesting_offer(
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    offer_task = context.user_data.pop(
        "interesting_offer_task",
        None,
    )
    if offer_task is not None and not offer_task.done():
        offer_task.cancel()


async def show_interesting_offer_after_pause(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    message_id: int,
    task_id: int,
    delay: int,
) -> None:
    try:
        await asyncio.sleep(delay)
        if (
            context.user_data.get("task_id") != task_id
            or "interesting_question_id" in context.user_data
        ):
            return
        offer_message = await context.bot.send_message(
            chat_id=chat_id,
            text="✨",
            reply_markup=ReplyKeyboardMarkup(
                [[INTERESTING_BUTTON_TEXT]],
                resize_keyboard=True,
                one_time_keyboard=True,
                input_field_placeholder="Можна трохи перемкнутися",
            ),
        )
        context.user_data["interesting_offer_message_id"] = (
            offer_message.message_id
        )
        context.user_data["interesting_offer_visible"] = True
    except asyncio.CancelledError:
        return
    except TelegramError:
        logging.warning(
            "Could not show the interesting-question offer",
            exc_info=True,
        )
    finally:
        current_task = context.user_data.get("interesting_offer_task")
        if current_task is asyncio.current_task():
            context.user_data.pop("interesting_offer_task", None)


def schedule_interesting_offer(
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    cancel_interesting_offer(context)
    chat_id = context.user_data.get("task_message_chat_id")
    message_id = context.user_data.get("task_message_id")
    task_id = context.user_data.get("task_id")
    if chat_id is None or message_id is None or task_id is None:
        return

    delay = interesting_offer_delay(context)
    context.user_data["interesting_offer_task"] = (
        context.application.create_task(
            show_interesting_offer_after_pause(
                context,
                chat_id,
                message_id,
                task_id,
                delay,
            ),
            name=f"interesting-offer-{message_id}",
        )
    )


async def clear_interesting_offer(
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    cancel_interesting_offer(context)
    was_visible = context.user_data.pop(
        "interesting_offer_visible",
        False,
    )
    offer_message_id = context.user_data.pop(
        "interesting_offer_message_id",
        None,
    )
    if not was_visible:
        return

    chat_id = context.user_data.get("task_message_chat_id")
    if chat_id is None:
        return

    try:
        control_message = await context.bot.send_message(
            chat_id=chat_id,
            text="\u2063",
            reply_markup=ReplyKeyboardRemove(),
        )
        await context.bot.delete_message(
            chat_id=chat_id,
            message_id=control_message.message_id,
        )
        if offer_message_id is not None:
            await context.bot.delete_message(
                chat_id=chat_id,
                message_id=offer_message_id,
            )
    except TelegramError:
        logging.warning(
            "Could not hide the interesting-question offer",
            exc_info=True,
        )


def set_new_task(
    context: ContextTypes.DEFAULT_TYPE,
) -> tuple[str, InlineKeyboardMarkup, int, int, int | None]:
    task_id = context.user_data.get("task_id", 0) + 1
    task_number = context.user_data.get("task_number", 0) + 1
    progress = {
        "subjects": context.user_data.setdefault("subjects", {})
    }
    english_progress = database.get_subject_progress(progress, "english")
    math_progress = database.get_subject_progress(progress, "math")
    english_level = max(
        1,
        min(english_progress["level"], ENGLISH_LEVEL_COUNT),
    )
    math_level = max(
        1,
        min(math_progress["level"], MATH_LEVEL_COUNT),
    )
    english_interval = max(3, 5 - min((english_level - 1) // 3, 2))
    math_modes = math_modes_for_level(math_level - 1)
    prefer_english = task_number % english_interval == 0

    if prefer_english:
        choice_count = min(3 + (english_level - 1) // 3, 6)
        question, answer, choices = generate_english_task(
            english_level - 1, choice_count
        )
        subject = "english"
        topic = ENGLISH_WORDS_TOPIC
        task_level = english_level
        subject_progress = english_progress
    else:
        choice_count = min(3 + (math_level - 1) // 3, 6)
        expression, numeric_answer, topic = generate_task(
            math_level - 1,
            math_modes,
        )
        question = f"{expression} = ?"
        answer = str(numeric_answer)
        choices = [
            str(choice)
            for choice in generate_choices(numeric_answer, choice_count)
        ]

        subject = "math"
        task_level = math_level
        subject_progress = math_progress

    context.user_data["answer"] = answer.casefold()
    context.user_data["task_id"] = task_id
    context.user_data["task_number"] = task_number
    context.user_data["task_expression"] = question
    context.user_data["task_type"] = subject
    context.user_data["task_topic"] = topic
    context.user_data["task_level"] = task_level
    context.user_data["task_choice_count"] = choice_count
    context.user_data["task_choices"] = list(choices)
    context.user_data["task_attempts"] = 0
    context.user_data.pop("last_wrong_emoji", None)
    bonus_target = adaptive_bonus_target(context, subject_progress)
    context.user_data["task_bonus_target_seconds"] = bonus_target

    return (
        question,
        answer_keyboard(choices, task_id),
        task_level,
        choice_count,
        bonus_target,
    )


async def send_new_task(
    message, context: ContextTypes.DEFAULT_TYPE, prefix: str = ""
) -> None:
    cancel_interesting_offer(context)
    question, keyboard, _, _, bonus_target = set_new_task(context)
    heading = f"{prefix}\n\n" if prefix else ""
    task_text = widen_short_message(f"{heading}{question}")
    context.user_data["task_text"] = task_text
    task_message = await message.reply_text(task_text, reply_markup=keyboard)
    context.user_data["task_started_at"] = monotonic()
    context.user_data["task_message_chat_id"] = task_message.chat_id
    context.user_data["task_message_id"] = task_message.message_id
    context.user_data["interesting_offer_visible"] = False
    schedule_interesting_offer(context)

    if bonus_target is not None:
        try:
            await task_message.set_reaction("⚡", is_big=True)
        except TelegramError:
            logging.warning(
                "Could not set the bonus reaction",
                exc_info=True,
            )
        else:
            context.user_data["bonus_reaction_chat_id"] = (
                task_message.chat_id
            )
            context.user_data["bonus_reaction_message_id"] = (
                task_message.message_id
            )
            elapsed = monotonic() - context.user_data["task_started_at"]
            delay = max(0.0, bonus_target - elapsed)
            context.user_data["bonus_reaction_task"] = (
                context.application.create_task(
                    expire_bonus_reaction(
                        context,
                        task_message.chat_id,
                        task_message.message_id,
                        context.user_data["task_id"],
                        delay,
                    ),
                    name=f"bonus-reaction-{task_message.message_id}",
                )
            )


async def expire_bonus_reaction(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    message_id: int,
    task_id: int,
    delay: float,
) -> None:
    try:
        await asyncio.sleep(delay)
        await context.bot.set_message_reaction(
            chat_id=chat_id,
            message_id=message_id,
            reaction=[],
        )
    except asyncio.CancelledError:
        return
    except TelegramError:
        logging.warning(
            "Could not remove the expired bonus reaction",
            exc_info=True,
        )
    finally:
        if context.user_data.get("task_id") == task_id:
            context.user_data.pop("bonus_reaction_task", None)
            context.user_data.pop("bonus_reaction_chat_id", None)
            context.user_data.pop("bonus_reaction_message_id", None)


async def clear_bonus_reaction(
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    reaction_task = context.user_data.pop("bonus_reaction_task", None)
    if reaction_task is not None and not reaction_task.done():
        reaction_task.cancel()

    chat_id = context.user_data.pop("bonus_reaction_chat_id", None)
    message_id = context.user_data.pop(
        "bonus_reaction_message_id", None
    )
    if chat_id is None or message_id is None:
        return

    try:
        await context.bot.set_message_reaction(
            chat_id=chat_id,
            message_id=message_id,
            reaction=[],
        )
    except TelegramError:
        logging.warning(
            "Could not clear the bonus reaction",
            exc_info=True,
        )


async def celebrate_correct(
    message,
    context: ContextTypes.DEFAULT_TYPE,
    earned_bonus: bool = False,
) -> None:
    emoji = "⚡🌟" if earned_bonus else random.choice(CORRECT_EMOJIS)
    await message.reply_text(emoji)
    await send_new_task(message, context, "Рухаємося далі:")


async def encourage_retry(message) -> None:
    await message.reply_text(random.choice(WRONG_EMOJIS))


async def save_answer(
    user,
    context: ContextTypes.DEFAULT_TYPE,
    selected_answer: str,
) -> tuple[bool, dict]:
    selected_answer = selected_answer.strip().casefold()
    correct_answer = context.user_data["answer"]
    is_correct = selected_answer == correct_answer
    answered_at = monotonic()
    started_at = context.user_data.get("task_started_at", answered_at)
    response_time_ms = max(0, round((answered_at - started_at) * 1000))
    attempts = context.user_data.get("task_attempts", 0)
    first_attempt = attempts == 0
    context.user_data["task_attempts"] = attempts + 1

    if database.is_enabled():
        try:
            progress, event = await database.record_answer(
                user=user,
                task_id=context.user_data["task_id"],
                expression=context.user_data["task_expression"],
                selected_answer=selected_answer,
                correct_answer=correct_answer,
                subject=context.user_data["task_type"],
                difficulty_level=context.user_data["task_level"],
                answer_options=context.user_data["task_choice_count"],
                response_time_ms=response_time_ms,
                first_attempt=first_attempt,
                offered_bonus_target_seconds=context.user_data.get(
                    "task_bonus_target_seconds"
                ),
            )
            context.user_data["subjects"] = progress["subjects"]
            update_warmup_state(
                context,
                event,
                is_correct,
                first_attempt,
                answered_at,
            )
            return is_correct, event
        except Exception:
            logging.exception(
                "Could not save answer to PostgreSQL; using memory fallback"
            )

    progress = {
        "subjects": context.user_data.get(
            "subjects", database.empty_progress()["subjects"]
        )
    }
    progress, event = database.apply_answer_to_progress(
        progress,
        context.user_data["task_type"],
        is_correct,
        response_time_ms,
        first_attempt,
        context.user_data.get("task_bonus_target_seconds"),
    )
    context.user_data["subjects"] = progress["subjects"]
    update_warmup_state(
        context,
        event,
        is_correct,
        first_attempt,
        answered_at,
    )

    return is_correct, event


def update_warmup_state(
    context: ContextTypes.DEFAULT_TYPE,
    event: dict,
    is_correct: bool,
    first_attempt: bool,
    answered_at: float,
) -> None:
    previous_answered_at = context.user_data.get(
        "warmup_last_answered_at"
    )
    if (
        previous_answered_at is None
        or answered_at - previous_answered_at > WARMUP_RESET_SECONDS
    ):
        warmup_points = 0
    else:
        warmup_points = context.user_data.get("warmup_points", 0)

    if first_attempt:
        if is_correct and event["timing_class"] == "normal":
            warmup_points = min(
                WARMUP_COMPLETE_POINTS,
                warmup_points + 1,
            )
        elif not is_correct or event["timing_class"] in {
            "slow",
            "suspicious",
        }:
            warmup_points = max(0, warmup_points - 1)

    context.user_data["warmup_points"] = warmup_points
    context.user_data["warmup_last_answered_at"] = answered_at


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await clear_bonus_reaction(context)
    await clear_interesting_offer(context)
    previous_answered_at = context.user_data.get(
        "warmup_last_answered_at"
    )
    keep_warmup = (
        previous_answered_at is not None
        and monotonic() - previous_answered_at <= WARMUP_RESET_SECONDS
    )
    warmup_points = (
        context.user_data.get("warmup_points", 0) if keep_warmup else 0
    )
    context.user_data.clear()
    try:
        progress = await database.get_progress(update.effective_user)
    except Exception:
        logging.exception(
            "Could not load progress from PostgreSQL; starting in memory"
        )
        progress = database.empty_progress()

    context.user_data.update(progress)
    context.user_data["warmup_points"] = warmup_points
    if keep_warmup:
        context.user_data["warmup_last_answered_at"] = (
            previous_answered_at
        )
    await update.message.reply_text(
        "Почнімо! Розв’язуй у своєму темпі — кількість спроб не обмежена.\n"
        "Обирай відповідь кнопкою. Якщо захочеш інший приклад, напиши /skip."
    )
    await send_new_task(update.message, context)


async def skip_task(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    await clear_bonus_reaction(context)
    await clear_interesting_offer(context)
    clear_interesting_state(context)
    previous_answer = context.user_data.get("answer")

    if previous_answer is None:
        prefix = "Ось перший приклад:"
    else:
        prefix = (
            f"Без проблем! Правильна відповідь була: {previous_answer}.\n"
            "Спробуймо інший:"
        )

    await send_new_task(update.message, context, prefix)


def clear_interesting_state(
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    for key in (
        "interesting_question_id",
        "interesting_callback_token",
        "interesting_prompt",
        "interesting_choices",
        "interesting_correct_answer",
        "interesting_explanation",
        "interesting_started_at",
        "interesting_steps_remaining",
    ):
        context.user_data.pop(key, None)


def interesting_keyboard(
    callback_token: str,
    choices: list[str],
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    choice,
                    callback_data=f"iq:{callback_token}:{index}",
                )
            ]
            for index, choice in enumerate(choices)
        ]
    )


def interesting_answer_feedback(
    is_correct: bool,
    explanation: str,
) -> str:
    if is_correct:
        return f"✅\n\n{explanation}"
    return "🤔"


async def send_interesting_question(
    message,
    context: ContextTypes.DEFAULT_TYPE,
    question: dict | None = None,
    user_id: int | None = None,
) -> bool:
    if question is None:
        question = await database.get_content_question(
            user_id=user_id,
        )
    if question is None:
        return False

    choices = list(question["answer_options"])
    random.shuffle(choices)
    callback_token = f"{random.getrandbits(32):08x}"
    context.user_data["interesting_question_id"] = question[
        "question_id"
    ]
    context.user_data["interesting_callback_token"] = callback_token
    context.user_data["interesting_prompt"] = question["prompt"]
    context.user_data["interesting_choices"] = choices
    context.user_data["interesting_correct_answer"] = question[
        "correct_answer"
    ]
    context.user_data["interesting_explanation"] = question[
        "explanation"
    ]

    await message.reply_text(
        f"✨ {question['prompt']}",
        reply_markup=interesting_keyboard(
            callback_token,
            choices,
        ),
    )
    context.user_data["interesting_started_at"] = monotonic()
    return True


async def handle_interesting_button(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    _, task_id_text = query.data.split(":", 1)
    task_id = int(task_id_text)

    if task_id != context.user_data.get("task_id"):
        await query.answer("Це завдання вже завершено 😊")
        return

    await query.answer()
    await start_interesting_route(
        query.message,
        query.from_user,
        context,
    )
    await query.edit_message_reply_markup(reply_markup=None)


async def start_interesting_route(
    message,
    user,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    await clear_interesting_offer(context)
    await clear_bonus_reaction(context)
    context.user_data["interesting_steps_remaining"] = (
        INTERESTING_ROUTE_MAX_STEPS
    )
    try:
        sent = await send_interesting_question(
            message,
            context,
            user_id=user.id,
        )
    except Exception:
        logging.exception("Could not load an interesting question")
        sent = False

    if not sent:
        clear_interesting_state(context)
        await send_new_task(message, context)


async def handle_interesting_answer(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    _, callback_token, choice_index_text = query.data.split(":", 2)

    if callback_token != context.user_data.get(
        "interesting_callback_token"
    ):
        await query.answer("Це питання вже завершено 😊")
        return

    question_id = context.user_data["interesting_question_id"]
    choices = context.user_data["interesting_choices"]
    choice_index = int(choice_index_text)
    if choice_index < 0 or choice_index >= len(choices):
        await query.answer("Варіант уже недоступний")
        return

    await query.answer()
    context.user_data.pop("interesting_callback_token", None)
    selected_answer = choices[choice_index]
    correct_answer = context.user_data[
        "interesting_correct_answer"
    ]
    is_correct = selected_answer == correct_answer
    started_at = context.user_data.get(
        "interesting_started_at",
        monotonic(),
    )
    response_time_ms = max(
        0,
        round((monotonic() - started_at) * 1_000),
    )

    try:
        await database.record_content_answer(
            user=query.from_user,
            question_id=question_id,
            selected_answer=selected_answer,
            correct_answer=correct_answer,
            response_time_ms=response_time_ms,
        )
    except Exception:
        logging.exception("Could not save an interesting answer")

    explanation = context.user_data["interesting_explanation"]
    feedback = interesting_answer_feedback(
        is_correct,
        explanation,
    )
    await query.edit_message_text(
        f"✨ {context.user_data['interesting_prompt']}\n\n"
        f"{feedback}"
    )

    remaining = (
        context.user_data.get("interesting_steps_remaining", 1) - 1
    )
    context.user_data["interesting_steps_remaining"] = remaining

    next_question = None
    if remaining > 0:
        try:
            next_question = await database.get_next_content_question(
                question_id,
                is_correct,
            )
        except Exception:
            logging.exception("Could not load the next graph question")

    if next_question is not None:
        await send_interesting_question(
            query.message,
            context,
            next_question,
        )
        context.user_data["interesting_steps_remaining"] = remaining
        return

    clear_interesting_state(context)
    await send_new_task(query.message, context)


async def handle_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    text = update.message.text.strip()

    if "interesting_question_id" in context.user_data:
        await update.message.reply_text("Обери варіант кнопкою 😊")
        return

    if text == INTERESTING_BUTTON_TEXT:
        await start_interesting_route(
            update.message,
            update.effective_user,
            context,
        )
        return

    if "answer" not in context.user_data:
        await update.message.reply_text("Напиши /start, щоб почати.")
        return

    if (
        context.user_data["task_type"] == "math"
        and not text.replace("-", "", 1).isdigit()
    ):
        await clear_bonus_reaction(context)
        await clear_interesting_offer(context)
        await update.message.reply_text("Введи число 😊")
        schedule_interesting_offer(context)
        return

    await clear_bonus_reaction(context)
    await clear_interesting_offer(context)
    is_correct, event = await save_answer(
        update.effective_user, context, text
    )

    if is_correct:
        await celebrate_correct(
            update.message, context, event["earned_bonus"]
        )
        return

    await encourage_retry(update.message)
    schedule_interesting_offer(context)


async def handle_answer_button(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    _, task_id_text, answer_text = query.data.split(":", 2)
    task_id = int(task_id_text)
    selected_answer = answer_text

    if task_id != context.user_data.get("task_id"):
        await query.answer("Цей приклад уже завершено 😊")
        return

    await query.answer()

    await clear_bonus_reaction(context)
    await clear_interesting_offer(context)
    is_correct, event = await save_answer(
        query.from_user, context, selected_answer
    )

    if is_correct:
        await query.edit_message_reply_markup(reply_markup=None)
        await celebrate_correct(
            query.message, context, event["earned_bonus"]
        )
        return

    previous_emoji = context.user_data.get("last_wrong_emoji")
    emoji = random.choice(
        [item for item in WRONG_EMOJIS if item != previous_emoji]
    )
    context.user_data["last_wrong_emoji"] = emoji
    await query.edit_message_text(
        f"{context.user_data['task_text']}\n\n{emoji}",
        reply_markup=answer_keyboard(
            context.user_data["task_choices"],
            task_id,
        ),
    )
    schedule_interesting_offer(context)


def main() -> None:
    bot_token = load_bot_token()
    app = (
        Application.builder()
        .token(bot_token)
        .post_init(database.connect_database)
        .post_shutdown(database.close_database)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("skip", skip_task))
    app.add_handler(
        CallbackQueryHandler(
            handle_interesting_button,
            pattern=r"^interesting:",
        )
    )
    app.add_handler(
        CallbackQueryHandler(
            handle_interesting_answer,
            pattern=r"^iq:",
        )
    )
    app.add_handler(
        CallbackQueryHandler(handle_answer_button, pattern=r"^answer:")
    )
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )

    external_url = os.getenv("RENDER_EXTERNAL_URL") or os.getenv("WEBHOOK_URL")
    if external_url:
        webhook_path = "telegram"
        webhook_secret = hashlib.sha256(bot_token.encode()).hexdigest()
        port = int(os.getenv("PORT", "10000"))

        logging.info("Starting webhook server on port %s", port)
        app.run_webhook(
            listen="0.0.0.0",
            port=port,
            url_path=webhook_path,
            webhook_url=f"{external_url.rstrip('/')}/{webhook_path}",
            secret_token=webhook_secret,
        )
    else:
        logging.info("Starting bot in polling mode")
        app.run_polling()


if __name__ == "__main__":
    main()
