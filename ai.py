import discord
from discord import Message
from discord.ext import commands
import requests
import asyncio
import re
import os
import threading
from datetime import datetime
from flask import Flask
from supabase import create_client, Client
from dotenv import load_dotenv
import concurrent.futures
import google.generativeai as genai

# Загружаем .env файл, если он существует
load_dotenv()

# Получаем переменные с приоритетом из переменных окружения Render
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN") 
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
TARGET_THREAD_ID = int(os.environ.get("TARGET_THREAD_ID", "0"))
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY")  # Ключ для Google Gemini API
MODEL = os.environ.get("MODEL")  # Модель по умолчанию

# Инициализация Google Generative AI клиента
genai.configure(api_key=GOOGLE_API_KEY)

# Flask-приложение для поддержания работы бота на Render.com
app = Flask(__name__)

executor = concurrent.futures.ThreadPoolExecutor(max_workers=5)

@app.route('/')
def home():
    return "Бот работает!", 200

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
   - Писать сообщения на весь экран нельзя.
2. При нарушении правил пользователем:
   - Вежливо отказывайся продолжать разговор
   - Не упоминай конкретные названия или имена
3. Как писать сообщения:
   - Если надо использовать гиперссылки, то используйте следующий формат: [текст](ссылка)
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

    # Показываем индикатор "печатает", чтобы пользователь видел, что бот обрабатывает запрос
    async with message.channel.typing():
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

        try:
            # Выбираем модель
            model = genai.GenerativeModel(MODEL)
            
            # Если есть изображение, отправляем с изображением
            if has_image:
                # Получаем URL изображения
                image_url = None
                for att in message.attachments:
                    if is_image_attachment(att):
                        image_url = att.url
                        break
                
                if image_url:
                    # Для мультимодального запроса
                    content = [
                        {'text': f"{SAFETY_PROMPT}\n\nПользовательский запрос: {user_text}"},
                        {'image_url': image_url}
                    ]
                    
                    # Функция для выполнения запроса с изображением
                    def make_api_request():
                        try:
                            response = model.generate_content(content)
                            return response
                        except Exception as e:
                            print(f"Ошибка API запроса: {e}")
                            return None
                else:
                    # Если изображение не удалось получить
                    await message.reply("Не удалось обработать изображение.")
                    return
            else:
                # Для текстового запроса используем историю
                bot.update_history(message.channel.id, "user", user_text)
                
                # Подготовка истории сообщений
                chat_history = []
                chat_history.append({"role": "system", "content": SAFETY_PROMPT})
                
                for msg in bot.conversation_history[message.channel.id]:
                    chat_history.append(msg)
                
                # Функция для выполнения запроса без изображения
                def make_api_request():
                    try:
                        response = model.generate_content(chat_history)
                        return response
                    except Exception as e:
                        print(f"Ошибка API запроса: {e}")
                        return None
            
            # Выполняем запрос в отдельном потоке
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(executor, make_api_request)
            
            if response and not response.candidates[0].finish_reason == "SAFETY":
                # Обрабатываем ответ
                raw_response = response.text
                raw_response = strip_think(raw_response)
                
                # Проверяем контент ответа
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
                else:
                    await message.reply("Я получил пустой ответ. Пожалуйста, попробуйте переформулировать вопрос.")
            elif response and response.candidates[0].finish_reason == "SAFETY":
                await message.reply("Я не могу ответить на этот вопрос из соображений безопасности.")
            else:
                error_msg = "Не удалось получить ответ от API."
                if response and hasattr(response, 'error'):
                    error_msg = f"Ошибка API: {response.error}"
                
                print(error_msg)
                await message.reply(f"Произошла ошибка: {error_msg}")
                
        except Exception as e:
            print(f"Произошла ошибка: {str(e)}")
            await message.reply(f"Произошла ошибка при обработке запроса: {str(e)}")

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
        
    if not GOOGLE_API_KEY:
        print("ОШИБКА: API ключ Google Gemini не найден в переменных окружения Render.com")
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
