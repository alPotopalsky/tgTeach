# MathBot

Простий Telegram-бот для тренування арифметики. Він генерує приклади на
додавання, віднімання та множення й дає три спроби на кожну відповідь.

## Запуск

Потрібен Python 3.10 або новіший.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item token.example.txt token.txt
```

Замініть текст у `token.txt` на токен свого Telegram-бота, а потім запустіть:

```powershell
python mathbot.py
```

Замість `token.txt` можна задати змінну середовища `BOT_TOKEN`.

## Використання

Відкрийте чат із ботом і надішліть команду `/start`.
