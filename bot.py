import os
import asyncio
import logging
from collections import deque
from dotenv import load_dotenv
import yt_dlp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
DOWNLOADS_DIR = "downloads"
os.makedirs(DOWNLOADS_DIR, exist_ok=True)
logging.basicConfig(level=logging.INFO)

TG_MAX_SIZE = 50 * 1024 * 1024
TG_MAX_SIZE_BOT = 2000 * 1024 * 1024

FFMPEG_DIR = r"C:\Users\dmitr\Desktop\Новая папка"

user_urls: dict[int, str] = {}
download_queue: deque = deque()
queue_running = False


def format_size(bytes_: int) -> str:
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if bytes_ < 1024:
            return f"{bytes_:.1f} {unit}"
        bytes_ /= 1024
    return f"{bytes_:.1f} ТБ"

def format_duration(seconds: int) -> str:
    h, m, s = seconds // 3600, (seconds % 3600) // 60, seconds % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

def progress_bar(percent: float, width: int = 10) -> str:
    filled = int(width * percent / 100)
    return "█" * filled + "░" * (width - filled)


def get_video_info(url: str) -> dict | None:
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True}) as ydl:
            return ydl.extract_info(url, download=False)
    except Exception:
        return None


def make_progress_hook(loop, status_msg, chat_id):
    """Возвращает хук прогресса — обновляет сообщение каждые 5%"""
    last_percent = [-1]

    def hook(d):
        if d["status"] != "downloading":
            return
        total = d.get("total_bytes") or d.get("total_bytes_estimate", 0)
        downloaded = d.get("downloaded_bytes", 0)
        if not total:
            return
        percent = downloaded / total * 100
        if percent - last_percent[0] < 5:
            return
        last_percent[0] = percent
        bar = progress_bar(percent)
        text = (
            f"⏳ Скачиваю...\n"
            f"{bar} {percent:.0f}%\n"
            f"{format_size(downloaded)} / {format_size(total)}"
        )
        asyncio.run_coroutine_threadsafe(
            status_msg.edit_text(text), loop
        )

    return hook


def sanitize_filename(name: str) -> str:
    """Убираем символы которые нельзя использовать в именах файлов"""
    for ch in r'\/:*?"<>|':
        name = name.replace(ch, "_")
    return name[:100]


def download_video(url: str, quality: str, hook) -> str:
    fmt = (
        "bestvideo+bestaudio/best"
        if quality == "best"
        else f"bestvideo[height<={quality}]+bestaudio/best[height<={quality}]/best"
    )
    options = {
        "format": fmt,
        "outtmpl": f"{DOWNLOADS_DIR}/%(title)s.%(ext)s",
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "progress_hooks": [hook],
        "ffmpeg_location": FFMPEG_DIR,
        "restrictfilenames": False,
        "windowsfilenames": True,
    }
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=True)
        base = ydl.prepare_filename(info).rsplit(".", 1)[0]
        return base + ".mp4"


def download_audio(url: str, quality: str, hook) -> str:
    bitrate = {"low": "128", "medium": "192", "high": "320"}.get(quality, "192")
    options = {
        "format": "bestaudio/best",
        "outtmpl": f"{DOWNLOADS_DIR}/%(title)s.%(ext)s",
        "quiet": True,
        "no_warnings": True,
        "progress_hooks": [hook],
        "ffmpeg_location": FFMPEG_DIR,
        "windowsfilenames": True,
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": bitrate,
        }, {
            "key": "FFmpegMetadata",
        }],
    }
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=True)
        base = ydl.prepare_filename(info).rsplit(".", 1)[0]
        return base + ".mp3"


async def send_file(message, path: str, fmt: str):
    size = os.path.getsize(path)
    filename = os.path.basename(path)
    title = filename.rsplit(".", 1)[0]

    if size <= TG_MAX_SIZE:
        with open(path, "rb") as f:
            if fmt == "video":
                await message.reply_video(video=f, supports_streaming=True, filename=filename)
            else:
                await message.reply_audio(audio=f, title=title, filename=filename)

    else:
        await message.reply_text(
            f"⚠️ Файл весит {format_size(size)} — больше лимита Telegram (50 МБ).\n"
            f"Разбиваю на части по 45 МБ..."
        )
        part_size = 45 * 1024 * 1024
        part_num = 1
        parts = []

        with open(path, "rb") as f:
            while chunk := f.read(part_size):
                part_path = path.replace(".mp4", f"_part{part_num}.mp4") \
                                 .replace(".mp3", f"_part{part_num}.mp3")
                with open(part_path, "wb") as pf:
                    pf.write(chunk)
                parts.append(part_path)
                part_num += 1

        total = len(parts)
        for i, part_path in enumerate(parts, 1):
            await message.reply_text(f"📦 Отправляю часть {i}/{total}...")
            with open(part_path, "rb") as f:
                if fmt == "video":
                    await message.reply_document(
                        document=f,
                        filename=os.path.basename(part_path),
                        caption=f"Часть {i} из {total}",
                    )
                else:
                    await message.reply_audio(audio=f, title=f"Часть {i}/{total}")
            os.remove(part_path)


async def process_queue(app):
    """Обрабатывает задачи из очереди по одной"""
    global queue_running
    queue_running = True

    while download_queue:
        task = download_queue.popleft()
        url      = task["url"]
        fmt      = task["fmt"]
        quality  = task["quality"]
        message  = task["message"]
        loop     = task["loop"]

        status_msg = await message.reply_text("⏳ Начинаю скачивание...")
        hook = make_progress_hook(loop, status_msg, message.chat_id)

        try:
            if fmt == "video":
                path = await asyncio.get_event_loop().run_in_executor(
                    None, download_video, url, quality, hook
                )
            else:
                path = await asyncio.get_event_loop().run_in_executor(
                    None, download_audio, url, quality, hook
                )

            await status_msg.edit_text("✅ Готово! Отправляю...")
            await send_file(message, path, fmt)
            os.remove(path)

        except Exception as e:
            logging.error(e)
            await status_msg.edit_text(
                f"❌ Ошибка:\n<code>{e}</code>", parse_mode="HTML"
            )

    queue_running = False


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Привет! Отправь ссылку на видео.\n\n"
        "Поддерживаю YouTube, Instagram, TikTok, Twitter/X и 1000+ сайтов.\n"
        "Файлы > 50 МБ разобью на части автоматически."
    )


async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()
    user_id = update.effective_user.id
    user_urls[user_id] = url

    await update.message.reply_text("🔍 Получаю информацию о видео...")
    info = get_video_info(url)

    if info:
        title    = info.get("title", "Без названия")
        uploader = info.get("uploader") or info.get("channel", "Неизвестно")
        duration = info.get("duration")
        view_count = info.get("view_count")
        thumbnail = info.get("thumbnail")

        lines = [f"🎬 <b>{title}</b>", f"👤 {uploader}"]
        if duration:
            lines.append(f"⏱ {format_duration(int(duration))}")
        if view_count:
            lines.append(f"👁 {view_count:,} просмотров")

        preview_text = "\n".join(lines) + "\n\nЧто скачать?"

        keyboard = [[
            InlineKeyboardButton("🎬 Видео", callback_data="format:video"),
            InlineKeyboardButton("🎵 Аудио (MP3)", callback_data="format:audio"),
        ]]

        if thumbnail:
            try:
                await update.message.reply_photo(
                    photo=thumbnail,
                    caption=preview_text,
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                )
                return
            except Exception:
                pass

        await update.message.reply_text(
            preview_text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    else:
        keyboard = [[
            InlineKeyboardButton("🎬 Видео", callback_data="format:video"),
            InlineKeyboardButton("🎵 Аудио (MP3)", callback_data="format:audio"),
        ]]
        await update.message.reply_text(
            "📎 Ссылка получена. Что скачать?",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global queue_running
    query = update.callback_query
    await query.answer()
    data    = query.data
    user_id = query.from_user.id

    if data == "format:video":
        keyboard = [
            [
                InlineKeyboardButton("📱 360p",  callback_data="quality:video:360"),
                InlineKeyboardButton("🖥 480p",  callback_data="quality:video:480"),
            ],
            [
                InlineKeyboardButton("📺 720p",  callback_data="quality:video:720"),
                InlineKeyboardButton("🎥 1080p", callback_data="quality:video:1080"),
            ],
            [InlineKeyboardButton("⚡ Максимальное", callback_data="quality:video:best")],
        ]
        await query.edit_message_caption(
            caption="🎬 Выбери качество видео:",
            reply_markup=InlineKeyboardMarkup(keyboard),
        ) if query.message.photo else await query.edit_message_text(
            "🎬 Выбери качество видео:",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    elif data == "format:audio":
        keyboard = [[
            InlineKeyboardButton("🔉 128k", callback_data="quality:audio:low"),
            InlineKeyboardButton("🔊 192k", callback_data="quality:audio:medium"),
            InlineKeyboardButton("🎧 320k", callback_data="quality:audio:high"),
        ]]
        await query.edit_message_caption(
            caption="🎵 Выбери качество аудио:",
            reply_markup=InlineKeyboardMarkup(keyboard),
        ) if query.message.photo else await query.edit_message_text(
            "🎵 Выбери качество аудио:",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    elif data.startswith("quality:"):
        _, fmt, quality = data.split(":")
        url = user_urls.get(user_id)

        if not url:
            await query.message.reply_text("❌ Ссылка не найдена, отправь ещё раз.")
            return

        pos = len(download_queue) + (1 if queue_running else 0)
        queue_notice = f"\n📋 Позиция в очереди: {pos + 1}" if pos > 0 else ""

        await query.message.reply_text(
            f"✅ Задача добавлена!{queue_notice}\nОжидай..."
        )

        download_queue.append({
            "url":     url,
            "fmt":     fmt,
            "quality": quality,
            "message": query.message,
            "loop":    asyncio.get_event_loop(),
        })

        if not queue_running:
            asyncio.create_task(process_queue(context.application))


if __name__ == "__main__":
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))
    app.add_handler(CallbackQueryHandler(handle_callback))
    print("✅ Бот запущен...")
    app.run_polling()