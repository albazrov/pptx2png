import sys
import logging
import asyncio
import shutil
import configparser
from pathlib import Path

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart
from aiogram.types import FSInputFile, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
import aiohttp

# Test CI/CD 2

# ИМПОРТ НАШИХ КАСТОМНЫХ МОДУЛЕЙ
import converter_engine
from user_manager import UserManager

# Конфигурация путей в оперативной памяти
SHM_DIR = Path("/dev/shm/pptx2png_tasks")
SHM_DIR.mkdir(exist_ok=True)

# Инициализация конфигурации .ini
config_path = Path.cwd() / "config.ini"
config = configparser.ConfigParser()
if not config_path.exists():
    sys.exit("❌ Ошибка: Файл config.ini не найден!")

config.read(config_path, encoding='utf-8')
try:
    BOT_TOKEN = config.get("Telegram", "BOT_TOKEN").strip()
    ADMIN_ID = int(config.get("Telegram", "ADMIN_ID").strip())
except Exception as e:
    sys.exit(f"❌ Ошибка в config.ini: {e}")

# Инициализация менеджера пользователей
user_mgr = UserManager(admin_id=ADMIN_ID)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

def get_settings_keyboard(user_id):
    cfg = user_mgr.get_user_config(user_id)
    q_std = "✅ Standard" if cfg["quality"] == "standard" else "Standard"
    q_2k  = "✅ 2K" if cfg["quality"] == "2k" else "2K"
    q_4k  = "✅ 4K" if cfg["quality"] == "4k" else "4K"
    pdf_status = "✅ Да (ZIP + PDF)" if cfg["keep_pdf"] else "❌ Нет (Только ZIP)"
    
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text=q_std, callback_data="set_q_standard"),
        InlineKeyboardButton(text=q_2k, callback_data="set_q_2k"),
        InlineKeyboardButton(text=q_4k, callback_data="set_q_4k")
    )
    builder.row(InlineKeyboardButton(text=f"Возвращать PDF: {pdf_status}", callback_data="toggle_pdf"))
    return builder.as_markup()

async def check_access(message: types.Message) -> bool:
    if message.from_user.id in user_mgr.load_allowed_users():
        return True

    username = f"@{message.from_user.username}" if message.from_user.username else "нет юзернейма"
    admin_kb = InlineKeyboardBuilder()
    admin_kb.row(
        InlineKeyboardButton(text="✅ Разрешить", callback_data=f"adm_allow_{message.from_user.id}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"adm_deny_{message.from_user.id}")
    )
    try:
        await bot.send_message(
            chat_id=ADMIN_ID,
            text=f"🔔 **Запрос доступа!**\n\n• {message.from_user.full_name}\n• {username}\n• ID: `{message.from_user.id}`",
            parse_mode="Markdown", reply_markup=admin_kb.as_markup()
        )
        await message.reply("🔒 Доступ ограничен. Администратору отправлен запрос.")
    except Exception:
        await message.reply("🔒 Доступ ограничен. Ошибка уведомления админа.")
    return False

@dp.callback_query(F.data.startswith("adm_"))
async def handle_admin_decision(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    data = callback.data.split("_")
    action, target_id = data[1], int(data[2])

    if action == "allow":
        user_mgr.save_allowed_user(target_id)
        await callback.message.edit_text(f"✅ Доступ для `{target_id}` одобрен.")
        try: await bot.send_message(target_id, "🎉 Доступ одобрен! Нажмите /start.")
        except Exception: pass
    elif action == "deny":
        await callback.message.edit_text(f"❌ Запрос `{target_id}` отклонен.")
        try: await bot.send_message(target_id, "❌ Доступ отклонен.")
        except Exception: pass
    await callback.answer()

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    if not await check_access(message): return
    await message.reply("👋 Привет! Настройте параметры генерации:", reply_markup=get_settings_keyboard(message.from_user.id))

@dp.callback_query(F.data.startswith("set_q_"))
async def handle_quality_change(callback: types.CallbackQuery):
    new_q = callback.data.replace("set_q_", "")
    user_mgr.get_user_config(callback.from_user.id)["quality"] = new_q
    await callback.message.edit_reply_markup(reply_markup=get_settings_keyboard(callback.from_user.id))
    await callback.answer(f"Качество: {new_q.upper()}")

@dp.callback_query(F.data == "toggle_pdf")
async def handle_pdf_toggle(callback: types.CallbackQuery):
    cfg = user_mgr.get_user_config(callback.from_user.id)
    cfg["keep_pdf"] = not cfg["keep_pdf"]
    await callback.message.edit_reply_markup(reply_markup=get_settings_keyboard(callback.from_user.id))
    await callback.answer(f"Возврат PDF {'включен' if cfg['keep_pdf'] else 'выключен'}.")

async def download_file_by_url(url: str, destination: Path, status_message: types.Message) -> bool:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                if response.status != 200:
                    await status_message.edit_text(f"❌ Ошибка загрузки. Статус: {response.status}")
                    return False
                with open(destination, 'wb') as f:
                    while True:
                        chunk = await response.content.read(1024 * 1024)
                        if not chunk: break
                        f.write(chunk)
        return True
    except Exception as e:
        await status_message.edit_text(f"❌ Ошибка HTTP-загрузки: {e}")
        return False

async def core_pipeline(downloaded_file_path: Path, status_message: types.Message, user_id: int):
    work_dir = downloaded_file_path.parent
    is_zip = downloaded_file_path.suffix.lower() == '.zip'
    cfg = user_mgr.get_user_config(user_id)

    try:
        if is_zip:
            await status_message.edit_text("📦 Распаковка ZIP...")
            pptx_path = converter_engine.extract_zip_if_needed(downloaded_file_path, work_dir)
            if not pptx_path:
                await status_message.edit_text("❌ Внутри ZIP не найдено .pptx.")
                return None, None
        else:
            pptx_path = downloaded_file_path

        await status_message.edit_text(f"⏳ Конвертация через LibreOffice в RAM...\n(Качество: {cfg['quality'].upper()})")
        
        clean_folder = not cfg["keep_pdf"]
        args = converter_engine.FakeArgs(
            quality=cfg["quality"], keep_pdf=cfg["keep_pdf"], 
            dark_mode=True, zip_mode=True, clean=clean_folder, output_dir=str(work_dir)
        )
        
        # Вызов движка в фоновом пуле потоков
        await asyncio.to_thread(converter_engine.process_file_local, pptx_path, args)
        
        expected_zip = next(work_dir.glob("*.zip"), None) if not is_zip else [f for f in work_dir.glob("*.zip") if f != downloaded_file_path][0]
        
        final_pdf_path = None
        if cfg["keep_pdf"]:
            png_folder = next((d for d in work_dir.iterdir() if d.is_dir() and d.name.endswith("_output")), None)
            if png_folder:
                pdf_file = next(png_folder.glob("*.pdf"), None)
                if pdf_file:
                    final_pdf_path = work_dir / f"{pptx_path.stem}.pdf"
                    shutil.move(str(pdf_file), str(final_pdf_path))
                shutil.rmtree(png_folder)

        return expected_zip, final_pdf_path
    except Exception as e:
        logging.error(f"Критическая ошибка ядра: {e}")
        return None, None

@dp.message(F.document)
async def handle_docs(message: types.Message):
    if not await check_access(message): return
    file_name = message.document.file_name
    if Path(file_name).suffix.lower() not in ['.pptx', '.ppt', '.zip']:
        await message.reply("❌ Неверный формат.")
        return

    user_id = message.from_user.id
    task_dir = SHM_DIR / f"task_{user_id}_{message.message_id}"
    task_dir.mkdir(exist_ok=True)
    status_message = await message.reply("📥 Загрузка файла в RAM...")
    try:
        download_path = task_dir / file_name
        await bot.download(file=message.document.file_id, destination=str(download_path))
        output_zip, output_pdf = await core_pipeline(download_path, status_message, user_id)
        
        if output_zip:
            await status_message.edit_text("📤 Отправка результатов...")
            await message.reply_document(document=FSInputFile(path=output_zip), caption="📦 ZIP с картинками готов!")
            if output_pdf:
                await message.reply_document(document=FSInputFile(path=output_pdf), caption="📄 PDF готов!")
            await status_message.delete()
            
            # # АВТОМАТИЧЕСКИЙ ВЫВОД КНОПОК ПОСЛЕ ОТПРАВКИ
            await message.answer("⚙️ **Настройки для следующей презентации:**", reply_markup=get_settings_keyboard(user_id))
        else:
            await status_message.edit_text("❌ Ошибка сборки файлов.")
    except Exception as e:
        if "file is too big" in str(e).lower() or "bad request" in str(e).lower():
            await status_message.edit_text("❌ Ошибка: Файл превышает лимит Telegram (20 МБ).\nИспользуйте отправку ссылкой Google Drive.")
        else:
            await status_message.edit_text(f"❌ Ошибка: {e}")
    finally:
        if task_dir.exists(): shutil.rmtree(task_dir)

@dp.message(F.text.contains("http://") | F.text.contains("https://"))
async def handle_links(message: types.Message):
    if not await check_access(message): return
    direct_url = converter_engine.convert_to_direct_download(message.text)
    
    user_id = message.from_user.id
    task_dir = SHM_DIR / f"task_{user_id}_{message.message_id}"
    task_dir.mkdir(exist_ok=True)
    status_message = await message.reply("🌐 Скачивание ссылки в RAM...")
    try:
        download_path = task_dir / "downloaded_presentation.pptx"
        if await download_file_by_url(direct_url, download_path, status_message):
            output_zip, output_pdf = await core_pipeline(download_path, status_message, user_id)
            if output_zip:
                await status_message.edit_text("📤 Отправка результатов...")
                await message.reply_document(document=FSInputFile(path=output_zip), caption="📦 ZIP готов!")
                if output_pdf:
                    await message.reply_document(document=FSInputFile(path=output_pdf), caption="📄 PDF готов!")
                await status_message.delete()
                
                # # АВТОМАТИЧЕСКИЙ ВЫВОД КНОПОК ПОСЛЕ ОТПРАВКИ
                await message.answer("⚙️ **Настройки для следующей презентации:**", reply_markup=get_settings_keyboard(user_id))
            else:
                await status_message.edit_text("❌ Ошибка конвертации по ссылке.")
    except Exception as e:
        await status_message.edit_text(f"❌ Ошибка ссылки: {e}")
    finally:
        if task_dir.exists(): shutil.rmtree(task_dir)
@dp.message(F.text & ~F.text.contains("http://") & ~F.text.contains("https://"))
async def handle_any_text(message: types.Message):
    """Если пользователь пишет любой обычный текст (не ссылку), бот выводит актуальные настройки."""
    if not await check_access(message): 
        return
        
    user_id = message.from_user.id
    await message.reply(
        "⚙️ **Параметры генерации слайдов:**\n\n"
        "Настройте качество картинок и режим сохранения PDF перед отправкой презентации.", 
        reply_markup=get_settings_keyboard(user_id)
    )

async def main():
    """Основная функция запуска Telegram-бота."""
    logging.info("Модульный бот запущен на /dev/shm...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try: 
        asyncio.run(main())
    except KeyboardInterrupt: 
        logging.info("Бот остановлен.")


