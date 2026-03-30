import asyncio
import os
import sys
import json
import requests
from flask import Flask, request
from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes
from cryptography.fernet import Fernet
import psycopg2
from psycopg2.extras import Json
import threading

try:
    from dotenv import load_dotenv
    load_dotenv()
    print("Загружены переменные из .env")
except ImportError:
    pass  # если библиотека не установлена, просто игнорируем
# ==================== КОНФИГУРАЦИЯ ====================
# Переменные окружения
CLIENT_ID = os.getenv("TWITCH_CLIENT_ID")
CLIENT_SECRET = os.getenv("TWITCH_CLIENT_SECRET")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")          # будет задана Render автоматически
ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY")      # опционально: если не задан, сгенерируем и сохраним в БД

# Для локального запуска можно не проверять DATABASE_URL
if not all([CLIENT_ID, CLIENT_SECRET, TELEGRAM_BOT_TOKEN]):
    raise ValueError("Отсутствуют необходимые переменные окружения")

# Глобальные переменные
access_token = None
previous_status = {}        # {username: bool}
lock = asyncio.Lock()
cipher = None

# Flask для health check
flask_app = Flask(__name__)

@flask_app.route('/health')
def health():
    return "OK", 200

def run_flask():
    flask_app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 10000)))

# Запускаем Flask в отдельном потоке
threading.Thread(target=run_flask, daemon=True).start()
# =====================================================

def get_db_connection():
    """Возвращает соединение с PostgreSQL."""
    return psycopg2.connect(DATABASE_URL)

def init_db():
    """Создаёт таблицы, если их нет."""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            # Таблица для подписок: chat_id (целое), users (json)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS subscriptions (
                    chat_id BIGINT PRIMARY KEY,
                    users JSONB NOT NULL DEFAULT '[]'::jsonb
                )
            """)
            # Таблица для ключа шифрования (одна строка)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS encryption_key (
                    id INTEGER PRIMARY KEY DEFAULT 1,
                    key TEXT
                )
            """)
            conn.commit()

def get_cipher():
    """Загружает или создаёт ключ шифрования из БД."""
    global cipher
    if cipher is not None:
        return cipher

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT key FROM encryption_key WHERE id = 1")
            row = cur.fetchone()
            if row and row[0]:
                key = row[0].encode()
            else:
                # Генерируем новый ключ
                key = Fernet.generate_key()
                cur.execute("INSERT INTO encryption_key (id, key) VALUES (1, %s) ON CONFLICT (id) DO UPDATE SET key = EXCLUDED.key", (key.decode(),))
                conn.commit()
                print("Создан новый ключ шифрования и сохранён в БД")
    cipher = Fernet(key)
    return cipher

def encrypt_chat_id(chat_id: int) -> str:
    """Шифрует chat_id и возвращает строку (base64)."""
    c = get_cipher()
    data = str(chat_id).encode()
    encrypted = c.encrypt(data)
    return encrypted.decode()

def decrypt_chat_id(encrypted_str: str) -> int:
    """Дешифрует строку и возвращает chat_id как int."""
    c = get_cipher()
    decrypted = c.decrypt(encrypted_str.encode())
    return int(decrypted.decode())

def load_subscriptions():
    """Загружает подписки из БД."""
    global subscriptions, previous_status
    subscriptions = {}
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT chat_id, users FROM subscriptions")
            rows = cur.fetchall()
            for chat_id, users_json in rows:
                subscriptions[chat_id] = json.loads(users_json)
    # Собираем всех стримеров для инициализации previous_status
    all_users = set()
    for users in subscriptions.values():
        all_users.update(users)
    previous_status = {user: False for user in all_users}
    print(f"Загружено подписок для {len(subscriptions)} чатов")

def save_subscriptions():
    """Сохраняет подписки в БД."""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            # Очищаем таблицу и вставляем заново (или можно обновлять по одному)
            cur.execute("DELETE FROM subscriptions")
            for chat_id, users in subscriptions.items():
                cur.execute(
                    "INSERT INTO subscriptions (chat_id, users) VALUES (%s, %s)",
                    (chat_id, json.dumps(users))
                )
            conn.commit()
    print(f"Сохранены подписки для {len(subscriptions)} чатов")

def get_app_access_token():
    """Получает App Access Token от Twitch."""
    url = "https://id.twitch.tv/oauth2/token"
    params = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "client_credentials"
    }
    try:
        resp = requests.post(url, params=params)
        resp.raise_for_status()
        data = resp.json()
        return data["access_token"]
    except Exception as e:
        print(f"Ошибка получения токена: {e}")
        return None

def update_token():
    global access_token
    token = get_app_access_token()
    if token:
        access_token = token
        print("Токен Twitch успешно обновлён")
    else:
        print("Не удалось получить токен Twitch")

def get_streams(user_logins, token):
    """Возвращает список стримов для заданных логинов."""
    if not token or not user_logins:
        return []
    headers = {
        "Client-ID": CLIENT_ID,
        "Authorization": f"Bearer {token}"
    }
    all_streams = []
    for i in range(0, len(user_logins), 100):
        chunk = user_logins[i:i+100]
        params = {"user_login": chunk}
        try:
            resp = requests.get("https://api.twitch.tv/helix/streams",
                                headers=headers, params=params)
            resp.raise_for_status()
            data = resp.json()
            all_streams.extend(data.get("data", []))
        except Exception as e:
            print(f"Ошибка запроса к Twitch API: {e}")
            continue
    return all_streams

async def send_telegram_message(chat_id, text):
    """Асинхронная отправка сообщения в Telegram."""
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    try:
        await bot.send_message(chat_id=chat_id, text=text)
        print(f"Сообщение отправлено в {chat_id}: {text[:50]}...")
    except Exception as e:
        print(f"Ошибка отправки в Telegram: {e}")

async def check_streams():
    """Проверяет статус стримов и отправляет уведомления всем подписанным."""
    global access_token, previous_status

    if not access_token:
        update_token()
        if not access_token:
            print("Нет токена, пропускаем проверку")
            return

    async with lock:
        all_subscribed_users = set()
        for users in subscriptions.values():
            all_subscribed_users.update(users)

    if not all_subscribed_users:
        print("Нет подписок, проверка не нужна")
        return

    streams = get_streams(list(all_subscribed_users), access_token)
    online_now = {stream["user_login"]: stream for stream in streams}

    for username in all_subscribed_users:
        was_online = previous_status.get(username, False)
        is_online = username in online_now

        if not was_online and is_online:
            stream = online_now[username]
            title = stream.get("title", "Без названия")
            game = stream.get("game_name", "Не указана")
            url = f"https://twitch.tv/{username}"
            message = (
                f"🔴 {username} начал стрим!\n"
                f"🎮 Игра: {game}\n"
                f"📝 {title}\n"
                f"🔗 {url}"
            )
            async with lock:
                chats_to_notify = [chat_id for chat_id, users in subscriptions.items() if username in users]
            for chat_id in chats_to_notify:
                await send_telegram_message(chat_id, message)

        previous_status[username] = is_online

# ==================== ОБРАБОТЧИКИ КОМАНД ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Привет! Я бот для уведомлений о стримах на Twitch.\n\n"
        "Доступные команды:\n"
        "/add <username> - добавить стримера\n"
        "/remove <username> - удалить стримера\n"
        "/list - показать список ваших подписок\n\n"
        "Уведомления будут приходить сюда."
    )

async def add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not context.args:
        await update.message.reply_text("Укажите логин стримера: /add username")
        return
    username = context.args[0].lower().strip()

    async with lock:
        if chat_id not in subscriptions:
            subscriptions[chat_id] = []
        user_subs = subscriptions[chat_id]

        if username in user_subs:
            await update.message.reply_text(f"Стример {username} уже в вашем списке.")
            return

        user_subs.append(username)
        save_subscriptions()

        if username not in previous_status:
            previous_status[username] = False

    if access_token:
        streams = get_streams([username], access_token)
        if streams:
            stream = streams[0]
            title = stream.get("title", "Без названия")
            game = stream.get("game_name", "Не указана")
            url = f"https://twitch.tv/{username}"
            message = (
                f"📺 {username} уже в эфире!\n"
                f"🎮 Игра: {game}\n"
                f"📝 {title}\n"
                f"🔗 {url}"
            )
            await update.message.reply_text(message)
            previous_status[username] = True
        else:
            await update.message.reply_text(f"Стример {username} добавлен. Бот будет отслеживать его стримы.")
    else:
        await update.message.reply_text(f"Стример {username} добавлен, но токен Twitch не получен. Проверьте настройки.")

async def remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not context.args:
        await update.message.reply_text("Укажите логин стримера: /remove username")
        return
    username = context.args[0].lower().strip()

    async with lock:
        if chat_id not in subscriptions:
            await update.message.reply_text(f"У вас нет подписок.")
            return
        user_subs = subscriptions[chat_id]
        if username not in user_subs:
            await update.message.reply_text(f"Стример {username} не найден в вашем списке.")
            return
        user_subs.remove(username)
        if not user_subs:
            del subscriptions[chat_id]
        save_subscriptions()

        still_subscribed = any(username in users for users in subscriptions.values())
        if not still_subscribed:
            previous_status.pop(username, None)

        await update.message.reply_text(f"Стример {username} удалён из вашего списка.")

async def list_subscriptions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    async with lock:
        user_subs = subscriptions.get(chat_id, [])
        if user_subs:
            text = "📺 Ваши подписки:\n" + "\n".join(f"- {user}" for user in user_subs)
        else:
            text = "Вы ещё не подписаны ни на одного стримера. Используйте /add username"
    await update.message.reply_text(text)

async def periodic_check(context: ContextTypes.DEFAULT_TYPE):
    await check_streams()

def main():
    init_db()
    load_subscriptions()
    update_token()

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("add", add))
    application.add_handler(CommandHandler("remove", remove))
    application.add_handler(CommandHandler("list", list_subscriptions))

    job_queue = application.job_queue
    if job_queue:
        job_queue.run_repeating(periodic_check, interval=300, first=10)
    else:
        print("JobQueue не доступен, проверки не будут выполняться автоматически")

    print("Бот запущен. Данные хранятся в PostgreSQL.")
    application.run_polling()

if __name__ == "__main__":
    main()