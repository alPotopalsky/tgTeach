import hashlib
import logging
import os
import random
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)


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

CORRECT_EMOJIS = ["🎉", "🥳", "🤩", "🏆", "🚀", "🌟"]
WRONG_EMOJIS = ["😅", "🙈", "🤔", "🫠", "🥲", "🧐"]


def generate_task(level: int = 0) -> tuple[str, int]:
    if level == 0:
        mode = random.choice(["add1", "sub1"])
    elif level == 1:
        mode = random.choice(["add2_small", "sub2_small", "mul_easy"])
    elif level == 2:
        mode = random.choice(["add2", "sub2", "mul"])
    else:
        mode = random.choice(["add3", "sub3", "mul"])

    if mode == "add1":
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
    else:
        a, b = random.randint(2, 15), random.randint(2, 15)
        operator = "×"
        answer = a * b

    return f"{a} {operator} {b}", answer


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


def answer_keyboard(
    choices: list[int], task_id: int
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


def set_new_task(
    context: ContextTypes.DEFAULT_TYPE,
) -> tuple[str, InlineKeyboardMarkup, int, int]:
    correct_count = context.user_data.get("correct_count", 0)
    level = min(correct_count // 3, 3)
    choice_count = min(3 + level, 6)
    expr, answer = generate_task(level)
    task_id = context.user_data.get("task_id", 0) + 1

    context.user_data["answer"] = answer
    context.user_data["task_id"] = task_id
    context.user_data.pop("last_wrong_emoji", None)
    choices = generate_choices(answer, choice_count)

    return expr, answer_keyboard(choices, task_id), level + 1, choice_count


async def send_new_task(
    message, context: ContextTypes.DEFAULT_TYPE, prefix: str = ""
) -> None:
    expr, keyboard, level, choice_count = set_new_task(context)
    heading = f"{prefix}\n\n" if prefix else ""
    task_text = (
        f"{heading}{expr} = ?\n\n"
        f"Рівень {level} · варіантів: {choice_count}"
    )
    context.user_data["task_text"] = task_text
    await message.reply_text(task_text, reply_markup=keyboard)


async def celebrate_correct(
    message, context: ContextTypes.DEFAULT_TYPE
) -> None:
    context.user_data["correct_count"] = (
        context.user_data.get("correct_count", 0) + 1
    )
    await message.reply_text(random.choice(CORRECT_EMOJIS))
    await send_new_task(message, context, "Готовий до наступного?")


async def encourage_retry(message) -> None:
    await message.reply_text(random.choice(WRONG_EMOJIS))


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.clear()
    context.user_data["correct_count"] = 0
    await update.message.reply_text(
        "Почнімо! Розв’язуй у своєму темпі — кількість спроб не обмежена.\n"
        "Обирай відповідь кнопкою. Якщо захочеш інший приклад, напиши /skip."
    )
    await send_new_task(update.message, context)


async def skip_task(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    previous_answer = context.user_data.get("answer")

    if previous_answer is None:
        prefix = "Ось перший приклад:"
    else:
        prefix = (
            f"Без проблем! Правильна відповідь була: {previous_answer}.\n"
            "Спробуймо інший:"
        )

    await send_new_task(update.message, context, prefix)


async def handle_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    text = update.message.text.strip()

    if "answer" not in context.user_data:
        await update.message.reply_text("Напиши /start, щоб почати.")
        return

    if not text.replace("-", "", 1).isdigit():
        await update.message.reply_text("Введи число 😊")
        return

    user_answer = int(text)
    correct = context.user_data["answer"]

    if user_answer == correct:
        await celebrate_correct(update.message, context)
        return

    await encourage_retry(update.message)


async def handle_answer_button(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    _, task_id_text, answer_text = query.data.split(":", 2)
    task_id = int(task_id_text)
    selected_answer = int(answer_text)

    if task_id != context.user_data.get("task_id"):
        await query.answer("Цей приклад уже завершено 😊")
        return

    await query.answer()

    if selected_answer == context.user_data["answer"]:
        await query.edit_message_reply_markup(reply_markup=None)
        await celebrate_correct(query.message, context)
        return

    previous_emoji = context.user_data.get("last_wrong_emoji")
    emoji = random.choice(
        [item for item in WRONG_EMOJIS if item != previous_emoji]
    )
    context.user_data["last_wrong_emoji"] = emoji
    await query.edit_message_text(
        f"{context.user_data['task_text']}\n\n{emoji}",
        reply_markup=query.message.reply_markup,
    )


def main() -> None:
    bot_token = load_bot_token()
    app = Application.builder().token(bot_token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("skip", skip_task))
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
