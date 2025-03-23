import discord
from discord import Message
from discord.ext import commands
import requests
import asyncio
import re
import os
import threading
import json
from datetime import datetime
from flask import Flask
from supabase import create_client, Client
from dotenv import load_dotenv
import aiohttp

# Загружаем .env файл, если он существует
load_dotenv()

# Получаем переменные с приоритетом из переменных окружения Render
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN") 
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
TARGET_THREAD_ID = int(os.environ.get("TARGET_THREAD_ID", "0"))
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY")
MODEL = os.environ.get("MODEL", "google/gemma-3-27b-it:free")
ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
# ID администратора для пинга при ошибках (замените на нужный ID)
ADMIN_ID = os.environ.get("ADMIN_ID", "")
# Flask-приложение для поддержания работы бота на Render.com
app = Flask(__name__)

@app.route('/')
def home():
    return "Бот работает", 200

# Получаем ID треда из переменных окружения
MAX_HISTORY_LENGTH = 10000
MAX_RESPONSE_LENGTH = 1950
MAX_RETRIES = 10

# Настройка Supabase
supabase: Client = None

if SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
        print("Подключено к Supabase!")
    except Exception as e:
        print(f"Ошибка подключения к Supabase: {e}")
else:
    print("ПРЕДУПРЕЖДЕНИЕ: Не указаны URL или ключ Supabase. Сохранение сообщений будет отключено.")

# Регулярное выражение для фильтрации запрещенного контента
CONTENT_FILTERS = re.compile(
    r'\b(суицид|самоубийств)\b',
    flags=re.IGNORECASE
)

# Системный промпт безопасности
SAFETY_PROMPT = """Ты профессиональный ассистент. Строгие правила:
1. Запрещено обсуждать:
   - Суицид, депрессию и методы самоповреждения
   - Любые сериалы/фильмы о запрещенной тематике
   - Расизм, нацистская символика, нацизм, фашизм.
   - Обсуждения/упоминания/разговоры политического характера.
   - Контент 18+ [ ники, аватарки, картинки ].
   - Притеснения по политическим, религиозным/ориентационным и личным взглядам.
   - Спам/Флуд/Оффтоп картинками, эмодзи, реакциями, символами и прочими вещами где-либо - запрещено.
   - Запрещено спамить пингами любых участников сервера.
   - Умышленное рекламирование своего или чужого ютуб-канала и прочего контента без разрешения @Volidorka или @Миса [ВПП] - запрещено
   - Нельзя писать команды с префиксом * например: *crime, и так далее и еще # например #ранг и еще + например +1
   - Все математические формулы и расчёты выводи в простом текстовом формате (например: P = F / A, без LaTeX или Markdown).
2. При нарушении правил пользователем:
   - Вежливо отказывайся продолжать разговор
   - Не упоминай конкретные названия или имена
3. Как писать сообщения:
   - Если надо использовать гиперссылки, то используйте следующий формат напримере: [вот ссылка на гугл](www.google.com)
   - Если надо использовать жирный текст, то используйте следующий формат: **жирный**
   - Если надо использовать курсивный текст, то используйте следующий формат: *курсив* или _курсив_
   - Если надо использовать зачеркнутый текст, то используйте следующий формат: ~~Перечеркнутый~~
   - Если надо использовать подчеркнутый текст, то используйте следующий формат: __подчеркнутый__
   - Если надо использовать заголовок, то используйте следующий формат: # Заголовок
   - Если надо использовать список, то используйте следующий формат: * пункт списка"""

def strip_think(text: str) -> str:
    """Удаляет блоки размышлений, заключённые в <think>...</think>."""
    return re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL).strip()

def is_image_attachment(attachment: discord.Attachment) -> bool:
    """Проверяет, является ли файл изображением по расширению."""
    return attachment.filename.lower().endswith((".png", ".jpg", ".jpeg", ".gif"))

class SafetyBot(commands.Bot):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.conversation_history = {}
    
    def deep_content_check(self, text: str) -> bool:
        """
        Проверка контента на запрещённые ключевые слова.
        Возвращает True, если нужно заблокировать ответ.
        """
        # Пример проверки: если есть совпадение по CONTENT_FILTERS, блокируем
        if CONTENT_FILTERS.search(text):
            return True
        return False

    async def format_response(self, text: str) -> str:
        """
        Триминг ответа по длине, если он слишком большой
        """
        cleaned = re.sub(r'\*{1,2}|_{1,2}|`{1,3}', '', text)
        sentences = re.split(r'(?<=[.!?])\s+', cleaned)
        filtered = []
        total_length = 0
        for sentence in sentences:
            if total_length + len(sentence) < MAX_RESPONSE_LENGTH - 3:
                filtered.append(sentence)
                total_length += len(sentence)
            else:
                break
        result = ' '.join(filtered).strip()
        if len(filtered) != len(sentences):
            result += "..."
        return result

    def update_history(self, thread_id: int, role: str, content: str):
        """Сохраняем историю переписки (только текстовые запросы/ответы)."""
        if thread_id not in self.conversation_history:
            self.conversation_history[thread_id] = []
        self.conversation_history[thread_id].append({"role": role, "content": content})

        # Если история слишком большая, обрезаем
        if len(self.conversation_history[thread_id]) > MAX_HISTORY_LENGTH * 2:
            self.conversation_history[thread_id] = self.conversation_history[thread_id][-MAX_HISTORY_LENGTH*2:]
    
    async def save_to_supabase(self, thread_id: int, author_id: int, content: str, is_bot: bool):
        """Сохраняет сообщение в Supabase"""
        if not supabase:
            return False
            
        try:
            message_data = {
                "thread_id": str(thread_id),
                "author_id": str(author_id),
                "content": content,
                "is_bot": is_bot,
                "created_at": datetime.now().isoformat()
            }
            
            result = supabase.table('messages').insert(message_data).execute()
            if hasattr(result, 'data') and result.data:
                return True
            return False
        except Exception as e:
            print(f"Ошибка при сохранении в Supabase: {str(e)}")
            return False

bot = SafetyBot(command_prefix="!", intents=discord.Intents.all())

@bot.event
async def on_ready():
    print(f"Бот {bot.user} готов к работе!")
    print(f"Настроен для работы по упоминаниям")

@bot.event
async def on_message(message: Message):
    # Игнорируем собственные сообщения бота
    if message.author == bot.user:
        return
    
    # Проверяем, упомянули ли бота в сообщении (пинг)
    bot_mentioned = bot.user.mentioned_in(message)
    if not bot_mentioned:
        # Если бота не упомянули, то игнорируем сообщение полностью
        return
    
    print(f"Бот упомянут в сообщении от {message.author.name} в канале {message.channel.id}")

    # Сохраняем сообщение пользователя в Supabase
    if supabase:
        await bot.save_to_supabase(
            message.channel.id, 
            message.author.id, 
            message.clean_content, 
            False
        )

    # Проверяем, есть ли вложения-изображения
    has_image = any(is_image_attachment(att) for att in message.attachments)

    # Удаляем блоки <think>...</think> из пользовательского текста
    user_text = strip_think(message.clean_content)
    print(f"Обработанный текст пользователя: {user_text}")

    # Проверка «запрещённого» текста (пример)
    if bot.deep_content_check(user_text):
        try:
            return await message.reply("Обсуждение данной темы запрещено правилами.")
        except discord.Forbidden:
            print("Нет прав отвечать в этот канал.")
        return

    # Если есть изображение, готовим запрос как vision
    if has_image:
        vision_instructions = (
"""Ты профессиональный ассистент. Строгие правила:
1. Запрещено обсуждать:
   - Суицид, депрессию и методы самоповреждения
   - Любые сериалы/фильмы о запрещенной тематике
   - Расизм, нацистская символика, нацизм, фашизм.
   - Обсуждения/упоминания/разговоры политического характера.
   - Контент 18+ [ ники, аватарки, картинки ].
   - Притеснения по политическим, религиозным/ориентационным и личным взглядам.
   - Спам/Флуд/Оффтоп картинками, эмодзи, реакциями, символами и прочими вещами где-либо - запрещено.
   - Запрещено спамить пингами любых участников сервера.
   - Умышленное рекламирование своего или чужого ютуб-канала и прочего контента без разрешения @Volidorka или @Миса [ВПП] - запрещено
   - Нельзя писать команды с префиксом * например: *crime, и так далее и еще # например #ранг и еще + например +1
   - Все математические формулы и расчёты выводи в простом текстовом формате (например: P = F / A, без LaTeX или Markdown).
2. При нарушении правил пользователем:
   - Вежливо отказывайся продолжать разговор
   - Не упоминай конкретные названия или имена
   - Предлагай обратиться к специалистам"""
        )
        combined_text = vision_instructions + (f"\nПользовательский запрос: {user_text}" if user_text else "")
        
        # Формируем «multimodal» контент (зависит от того, поддерживает ли модель формат type:image_url)
        vision_content = [{"type": "text", "text": combined_text}]
        for att in message.attachments:
            if is_image_attachment(att):
                vision_content.append({"type": "image_url", "image_url": {"url": att.url}})
                break

        payload = {
            "model": MODEL,
            "messages": [{"role": "user", "content": vision_content}],
            "temperature": 0.3,
            "max_tokens": 600,
            "frequency_penalty": 1.2,
            "presence_penalty": 0.9
        }
        endpoint = ENDPOINT
        headers = {
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json"
        }
    else:
        # Иначе это чисто текстовый запрос
        # Сохраняем в историю
        bot.update_history(message.channel.id, "user", user_text)
        payload = {
            "model": MODEL,
            "messages": (
                [{"role": "system", "content": SAFETY_PROMPT}]
                + bot.conversation_history[message.channel.id]
            ),
            "temperature": 0.3,
            "max_tokens": 600,
            "frequency_penalty": 1.2,
            "presence_penalty": 0.9
        }
        endpoint = ENDPOINT
        headers = {
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json"
        }

    # Отправляем «печатает...»
    async with message.channel.typing():
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(endpoint, headers=headers, json=payload, timeout=30) as response:
                    data = await response.json()
            
            # Проверяем HTTP-статус ответа
            if response.status_code != 200:
                error_message = f"Ошибка API: {response.status_code}"
                error_detail = "Неизвестная ошибка"
                
                try:
                    error_data = data
                    if "error" in error_data:
                        error_detail = error_data["error"].get("message", error_data["error"])
                except:
                    pass
                
                # Проверяем тип ошибки
                error_explanation = f"Ошибка OpenRouter API ({response.status_code}): {error_detail}"
                print(error_explanation)
                
                # Специальное сообщение для каждого типа ошибок
                if response.status_code == 401:
                    # Неверный API ключ
                    await message.reply(f"❌ Ошибка авторизации API: Неверный API ключ. '<@1023270857495281827>', пожалуйста, обнови нахуй API-ключ OpenRouter.")
                elif response.status_code == 429:
                    # Превышен лимит запросов
                    await message.reply(f"❌ Достигнут лимит запросов к API. '<@1023270857495281827>', пожалуйста, проверьте лимиты OpenRouter или обнови нахуй API-ключ.")
                elif response.status_code >= 500:
                    # Ошибка на стороне сервера
                    await message.reply("❌ Ошибка сервера OpenRouter. Пожалуйста, попробуйте позже.")
                else:
                    # Другие ошибки
                    await message.reply(f"❌ Произошла ошибка при обработке запроса: {error_detail}")
                
                try:
                    await message.add_reaction('❌')
                except discord.Forbidden:
                    print("Нет разрешения добавить реакцию ❌.")
                return
            
            # Обработка успешного ответа
            try:
                raw_response = data['choices'][0]['message']['content']
                raw_response = strip_think(raw_response)

                # Проверяем контент ответа (при желании)
                if bot.deep_content_check(raw_response):
                    try:
                        await message.reply("Не могу ответить на этот вопрос (контент запрещён).")
                    except discord.Forbidden:
                        print("Нет разрешения отправить ответ.")
                    # Если текстовый запрос, удаляем последний «user»
                    if not has_image and bot.conversation_history[message.channel.id]:
                        bot.conversation_history[message.channel.id].pop()
                    return

                # Форматируем ответ
                final_response = await bot.format_response(raw_response)
                if final_response.strip():
                    await message.reply(final_response)
                    
                    # Сохраняем ответ бота в Supabase
                    await bot.save_to_supabase(
                        message.channel.id,
                        bot.user.id,
                        final_response,
                        True
                    )
                    
                # Сохраняем ответ ассистента в историю (если это текстовый запрос)
                if not has_image:
                    bot.update_history(message.channel.id, "assistant", final_response)
            except Exception as e:
                print("Ошибка при разборе JSON:", data)
                await message.reply("❌ Ошибка обработки ответа от OpenRouter API.")
                await message.add_reaction('❌')
                return

        except requests.exceptions.Timeout:
            print("Превышено время ожидания ответа API")
            await message.reply("⏱️ Превышено время ожидания ответа от API. Пожалуйста, попробуйте позже.")
            await message.add_reaction('⏱️')
            return
        except requests.exceptions.RequestException as e:
            print(f"Ошибка при отправке запроса к API: {str(e)}")
            await message.reply(f"❌ Ошибка соединения с OpenRouter API: {str(e)}")
            await message.add_reaction('❌')
            return
        except Exception as e:
            print(f"Непредвиденная ошибка: {str(e)}")
            await message.reply("❌ Произошла непредвиденная ошибка при обработке запроса.")
            await message.add_reaction('❌')
            return

# Функция для запуска Flask-сервера
def run_flask_app():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

# Функция для запуска Discord-бота
def run_discord_bot():
    # Проверяем наличие необходимых переменных окружения
    if not DISCORD_TOKEN:
        print("ОШИБКА: Токен Discord не найден в переменных окружения Render.com")
        exit(1)
        
    if not OPENROUTER_API_KEY:
        print("ОШИБКА: API ключ OpenRouter не найден в переменных окружения Render.com")
        exit(1)
        
    if TARGET_THREAD_ID == 0:
        print("ОШИБКА: ID треда не найден в переменных окружения Render.com")
        exit(1)

    # Добавляем информацию о запуске обычного бота
    print("Попытка подключения обычного Discord бота")
    
    # Запуск бота
    bot.run(DISCORD_TOKEN)

# Запускаем оба процесса в разных потоках
if __name__ == '__main__':
    # Создаем и запускаем поток для Discord-бота
    discord_thread = threading.Thread(target=run_discord_bot)
    discord_thread.daemon = True
    discord_thread.start()
    
    # Запускаем Flask-сервер в основном потоке
    run_flask_app()
