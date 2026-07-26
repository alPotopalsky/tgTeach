import logging
import os
import random
from pathlib import Path

from telegram import Update
from telegram.ext import (
    Application,
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


def generate_task() -> tuple[str, int]:
    mode = random.choice(["add2", "sub2", "add3", "sub3", "mul"])

    if mode == "add2":
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
        operator = "*"
        answer = a * b

    return f"{a} {operator} {b}", answer


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    expr, answer = generate_task()
    context.user_data["answer"] = answer
    context.user_data["attempts"] = 3
    await update.message.reply_text(f"Почнімо! У тебе є 3 спроби.\n\n{expr} = ?")


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
    attempts = context.user_data.get("attempts", 3)

    if user_answer == correct:
        await update.message.reply_text("Молодець! ✔️ Правильно!")
        expr, answer = generate_task()
        context.user_data["answer"] = answer
        context.user_data["attempts"] = 3
        await update.message.reply_text(
            f"Новий приклад — знову 3 спроби!\n\n{expr} = ?"
        )
        return

    attempts -= 1
    context.user_data["attempts"] = attempts

    if attempts > 0:
        await update.message.reply_text(
            f"Невірно ❌\nСпробуй ще раз! (Залишилось спроб: {attempts})"
        )
        return

    await update.message.reply_text(
        f"На жаль, знову неправильно 😢\nПравильна відповідь: {correct}"
    )
    expr, answer = generate_task()
    context.user_data["answer"] = answer
    context.user_data["attempts"] = 3
    await update.message.reply_text(
        f"Наступний приклад — знову 3 спроби!\n\n{expr} = ?"
    )


def main() -> None:
    app = Application.builder().token(load_bot_token()).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )
    app.run_polling()


if __name__ == "__main__":
    main()
