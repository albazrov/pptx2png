import os
import shutil
import logging
import secrets
import asyncio
import zipfile
from pathlib import Path
from typing import Optional, Set, Dict, List, Tuple
from aiogram import Router, F, types, Bot
from aiogram.filters import CommandStart
from aiogram.types import InlineKeyboardButton, FSInputFile
from aiogram.utils.keyboard import InlineKeyboardBuilder

# Импортируем ВСЕ утилиты и конвейер обработки из utils.py
from utils import extract_text_from_pptx, check_spelling, download_file_by_url, core_pipeline
import converter_engine

# ==========================================
# ГЛОБАЛЬНЫЙ МЕНЕДЖЕР БЛОКИРОВОК ЗАДАЧ (С ОЧИСТКОЙ)
# ==========================================

class TaskLockManager:
    # ... (код без изменений, см. предыдущие версии) ...
    pass

# Создаём глобальный экземпляр менеджера блокировок
task_lock_manager = TaskLockManager()

# ==========================================
# ФОНОВАЯ ЗАДАЧА ДЛЯ ПЕРИОДИЧЕСКОЙ ОЧИСТКИ
# ==========================================

async def cleanup_loop():
    # ... (код без изменений) ...
    pass

# ==========================================
# ГЛОБАЛЬНЫЙ СЛОВАРЬ СЕССИЙ ПОЛЬЗОВАТЕЛЕЙ (НОВОЕ)
# ==========================================
user_sessions = {}  # {user_id: {"task_id": ..., "task_dir": ..., "file_path": ..., "awaiting_selection": False, "ranges": []}}

# Инициализируем единый роутер для этого модуля
router = Router()

# ==========================================
# 1. ВСПОМОГАТЕЛЬНАЯ ФУНКЦИЯ ДЛЯ БЕЗОПАСНОЙ ОБРАБОТКИ ИМЕНИ ФАЙЛА
# ==========================================

def safe_filename(filename: str) -> str:
    import re
    safe_name = os.path.basename(filename)
    safe_name = re.sub(r'[^\w\s.-]', '', safe_name)
    safe_name = re.sub(r'\s+', ' ', safe_name)
    safe_name = safe_name.strip()
    if not safe_name:
        safe_name = f"file_{secrets.token_hex(4)}"
    if len(safe_name) > 100:
        name, ext = os.path.splitext(safe_name)
        safe_name = name[:90] + ext
    return safe_name

def validate_download_path(task_dir: Path, destination: Path) -> bool:
    try:
        resolved_dest = destination.resolve()
        resolved_task = task_dir.resolve()
        return resolved_dest.parent == resolved_task or resolved_dest.parent in resolved_task.parents
    except Exception:
        return False

# ==========================================
# 2. ПАРСЕР ДИАПАЗОНОВ СЛАЙДОВ (НОВОЕ)
# ==========================================

def parse_slides_ranges(input_text: str) -> List[Tuple[int, int]]:
    """Парсит строку с номерами слайдов. Возвращает список кортежей (start, end)."""
    ranges = []
    parts = input_text.replace(" ", "").split(",")
    for part in parts:
        if not part:
            continue
        if "-" in part:
            try:
                start, end = map(int, part.split("-"))
                if start > end:
                    start, end = end, start
                ranges.append((start, end))
            except ValueError:
                return []
        else:
            try:
                num = int(part)
                ranges.append((num, num))
            except ValueError:
                return []
    return ranges

# ==========================================
# 3. ВСПОМОГАТЕЛЬНАЯ ФУНКЦИЯ КОНВЕРТАЦИИ В PNG (НОВОЕ)
# ==========================================

async def convert_all_pngs(pptx_path: Path, output_dir: Path, quality: str) -> List[Path]:
    """Конвертирует все слайды в PNG и возвращает список путей к PNG-файлам."""
    args = converter_engine.FakeArgs(
        quality=quality,
        keep_pdf=False,
        dark_mode=True,
        zip_mode=False,
        clean=False,
        output_dir=str(output_dir)
    )
    try:
        pdf_path = converter_engine.pptx_to_pdf_crossplatform(pptx_path, output_dir)
        total_slides, png_paths = converter_engine.pdf_to_png_fast(pdf_path, output_dir, quality)
        if pdf_path.exists():
            pdf_path.unlink()
        return png_paths
    except Exception as e:
        logging.error(f"Ошибка конвертации PNG: {e}", exc_info=True)
        return []

# ==========================================
# 4. ОБЩАЯ ФУНКЦИЯ ЗАПУСКА КОНВЕРТАЦИИ (НОВОЕ)
# ==========================================

async def run_conversion(callback: types.CallbackQuery, task_id: str, SHM_DIR: str, user_mgr, all_slides: bool = True, ranges: List[Tuple[int, int]] = None):
    """Запускает конвертацию слайдов (все или выбранные)."""
    user_id = callback.from_user.id
    chat_id = callback.message.chat.id
    session = user_sessions.get(user_id)
    if not session or session.get("task_id") != task_id:
        await callback.message.edit_text("❌ Сессия истекла. Отправьте файл заново.")
        return
    task_dir = session["task_dir"]
    pptx_path = session["file_path"]
    if not task_dir.exists() or not pptx_path.exists():
        await callback.message.edit_text("❌ Файл не найден. Отправьте заново.")
        return
    try:
        cfg = user_mgr.get_user_config(user_id)
        if all_slides:
            # Конвертация всех слайдов через существующий pipeline
            expected_zip, final_pdf_path = await core_pipeline(pptx_path, callback.message, user_id, user_mgr)
            if expected_zip and expected_zip.exists():
                await callback.message.edit_text("📤 Отправляю готовые файлы...")
                await callback.bot.send_document(chat_id=chat_id, document=FSInputFile(expected_zip),
                                                caption="📦 ZIP со всеми слайдами готов!")
                if final_pdf_path and final_pdf_path.exists():
                    await callback.bot.send_document(chat_id=chat_id, document=FSInputFile(final_pdf_path),
                                                    caption="📄 PDF готов!")
                await callback.message.delete()
            else:
                await callback.message.edit_text("❌ Ошибка конвертации всех слайдов.")
        elif ranges:
            # Конвертация выбранных слайдов – сначала получаем все PNG
            temp_png_dir = task_dir / "temp_pngs"
            temp_png_dir.mkdir(exist_ok=True)
            all_pngs = await convert_all_pngs(pptx_path, temp_png_dir, cfg["quality"])
            if not all_pngs:
                await callback.message.edit_text("❌ Не удалось конвертировать слайды в PNG.")
                return
            total_slides = len(all_pngs)
            archives = []
            for idx, (start, end) in enumerate(ranges, 1):
                if start > total_slides:
                    await callback.message.edit_text(f"❌ Слайд {start} не существует (всего {total_slides}).")
                    return
                if end > total_slides:
                    end = total_slides
                selected_files = []
                for i in range(start - 1, end):
                    if i < len(all_pngs):
                        selected_files.append(all_pngs[i])
                if not selected_files:
                    continue
                range_name = f"slides_{start}-{end}" if start != end else f"slide_{start}"
                zip_path = task_dir / f"{pptx_path.stem}_{range_name}.zip"
                with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
                    for file_path in selected_files:
                        zipf.write(file_path, arcname=file_path.name)
                archives.append(zip_path)
            if archives:
                await callback.message.edit_text(f"📤 Отправляю {len(archives)} архив(ов)...")
                for zip_path in archives:
                    if zip_path.exists():
                        await callback.bot.send_document(
                            chat_id=chat_id,
                            document=FSInputFile(zip_path),
                            caption=f"📦 {zip_path.name}"
                        )
                await callback.message.delete()
                await callback.bot.send_message(
                    chat_id=chat_id,
                    text="⚙️ **Настройки для следующей презентации:**",
                    reply_markup=get_settings_keyboard(user_id)
                )
            else:
                await callback.message.edit_text("❌ Ошибка создания архивов для выбранных слайдов.")
    except Exception as e:
        logging.error(f"Ошибка в run_conversion: {e}", exc_info=True)
        await callback.message.edit_text(f"❌ Ошибка конвертации: {e}")
    finally:
        # Очистка временных файлов
        if task_dir.exists():
            shutil.rmtree(task_dir)
        if user_id in user_sessions:
            del user_sessions[user_id]

# ==========================================
# 5. АДМИНСКИЕ ХЕНДЛЕРЫ (без изменений)
# ==========================================
@router.callback_query(F.data.startswith("adm_"))
async def handle_admin_decision(...):
    # ... (остаётся без изменений)
    pass

# ==========================================
# 6. КОМАНДА СТАРТ (без изменений)
# ==========================================
@router.message(CommandStart())
async def cmd_start(...):
    # ... (без изменений)
    pass

# ==========================================
# 7. НАСТРОЙКИ (без изменений)
# ==========================================
@router.callback_query(F.data.startswith("set_q_"))
async def handle_quality_settings(...):
    # ... (без изменений)
    pass

@router.callback_query(F.data == "toggle_pdf")
async def handle_toggle_pdf(...):
    # ... (без изменений)
    pass

# ==========================================
# 8. ГЕНЕРАЦИЯ ID ЗАДАЧИ (без изменений)
# ==========================================
def generate_task_id(...):
    # ... (без изменений)
    pass

def disable_task_buttons(task_id: str) -> InlineKeyboardBuilder:
    # ... (без изменений)
    pass

# ==========================================
# 9. ПРОВЕРКА ВЛАДЕЛЬЦА (без изменений)
# ==========================================
async def _validate_task_ownership(...):
    # ... (без изменений)
    pass

# ==========================================
# 10. ОБРАБОТЧИКИ ВЫБОРА СЛАЙДОВ (НОВЫЕ)
# ==========================================

@router.callback_query(F.data.startswith("slides_all:"))
async def handle_all_slides(callback: types.CallbackQuery, bot: Bot, SHM_DIR: str, user_mgr, check_access_by_user):
    """Пользователь выбрал все слайды."""
    if not await check_access_by_user(callback.from_user, bot):
        await callback.answer("❌ Доступ запрещен.", show_alert=True)
        return
    task_id = callback.data.split(":")[-1]
    # Сразу запускаем конвертацию всех слайдов
    await callback.message.edit_text("⚙️ Запускаю конвертацию всех слайдов...")
    await run_conversion(callback, task_id, SHM_DIR, user_mgr, all_slides=True)
    await callback.answer()

@router.callback_query(F.data.startswith("slides_select:"))
async def handle_select_slides(callback: types.CallbackQuery, bot: Bot):
    """Пользователь выбрал выборочную конвертацию."""
    task_id = callback.data.split(":")[-1]
    user_id = callback.from_user.id
    session = user_sessions.get(user_id)
    if session:
        session["awaiting_selection"] = True
        session["task_id"] = task_id
    await callback.message.edit_text(
        "📝 **Введите номера слайдов для конвертации.**\n\n"
        "Формат ввода:\n"
        "• Отдельные номера: `1, 3, 5, 7`\n"
        "• Диапазоны: `4-12, 15, 20-30`\n"
        "• Смешанный: `1, 3-5, 10, 15-20`\n\n"
        "Если укажете несколько диапазонов, каждый будет упакован в отдельный архив.",
        parse_mode="Markdown"
    )
    await callback.answer()

@router.callback_query(F.data.startswith("slides_convert:"))
async def handle_convert_selected(callback: types.CallbackQuery, bot: Bot, SHM_DIR: str, user_mgr, check_access_by_user):
    """Запуск конвертации выбранных слайдов."""
    if not await check_access_by_user(callback.from_user, bot):
        await callback.answer("❌ Доступ запрещен.", show_alert=True)
        return
    task_id = callback.data.split(":")[-1]
    user_id = callback.from_user.id
    session = user_sessions.get(user_id)
    if not session or session.get("task_id") != task_id:
        await callback.answer("❌ Сессия истекла.", show_alert=True)
        return
    ranges = session.get("ranges")
    if not ranges:
        await callback.answer("❌ Не выбраны слайды.", show_alert=True)
        return
    await callback.message.edit_text(f"⚙️ Запускаю конвертацию {len(ranges)} диапазон(ов)...")
    await run_conversion(callback, task_id, SHM_DIR, user_mgr, all_slides=False, ranges=ranges)
    await callback.answer()

# ==========================================
# 11. ОБРАБОТЧИКИ СПЕЛЛЕРА И КОНВЕРТАЦИИ (с изменениями)
# ==========================================

@router.callback_query(F.data.startswith("chk_spell:"))
async def callback_run_speller(...):
    # ... (код без изменений, но теперь он не удаляет task_dir)
    # ВАЖНО: в finally не удаляем task_dir, т.к. он нужен для конвертации.
    # Очистка произойдёт после конвертации или по таймауту.
    pass

@router.callback_query(F.data.startswith("chk_conv:"))
async def callback_run_conversion(...):
    # ... (код без изменений, но он должен использовать run_conversion?)
    # Можно оставить как есть, если хотим сохранить старый путь.
    # Но для единообразия лучше переделать на новую логику выбора слайдов.
    # Однако для обратной совместимости можно оставить старый обработчик,
    # который конвертирует все слайды.
    pass

# ==========================================
# 12. ОБРАБОТЧИКИ ФАЙЛОВ И ССЫЛОК (с изменениями для выбора слайдов)
# ==========================================

@router.message(F.document.file_name.lower().endswith(('.pptx', '.ppt')))
async def handle_pptx_document(message: types.Message, bot: Bot, SHM_DIR: str, check_access):
    if not await check_access(message):
        return

    document = message.document
    user_id = message.from_user.id
    chat_id = message.chat.id
    
    safe_file_name = safe_filename(document.file_name)
    if not safe_file_name.lower().endswith(('.pptx', '.ppt')):
        await message.reply("❌ Неверный формат файла. Отправьте PPTX или PPT.")
        return
    
    task_id = generate_task_id(chat_id, user_id, message.message_id)
    task_dir = Path(SHM_DIR) / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    
    ownership_file = task_dir / ".owner"
    ownership_file.write_text(f"{user_id}:{chat_id}")
    
    local_file_path = task_dir / safe_file_name
    if not validate_download_path(task_dir, local_file_path):
        await message.reply("❌ Ошибка безопасности: недопустимое имя файла.")
        return
    
    status_msg = await message.reply("⏳ Скачиваю презентацию в память...")
    
    try:
        file_info = await bot.get_file(document.file_id)
        await bot.download_file(file_info.file_path, destination=local_file_path)
        
        # Сохраняем сессию
        user_sessions[user_id] = {
            "task_id": task_id,
            "task_dir": task_dir,
            "file_path": local_file_path,
            "awaiting_selection": False,
            "ranges": []
        }
        
        # Показываем выбор слайдов
        kb = InlineKeyboardBuilder()
        kb.row(
            InlineKeyboardButton(text="📊 Все слайды", callback_data=f"slides_all:{task_id}"),
            InlineKeyboardButton(text="📝 Выбрать слайды", callback_data=f"slides_select:{task_id}")
        )
        
        await status_msg.edit_text(
            f"📄 **Файл '{safe_file_name}' успешно загружен.**\n\n"
            "Какие слайды конвертировать?\n"
            "• Все слайды (по умолчанию)\n"
            "• Выборочные (например: 1-5, 10, 15-20)\n\n"
            "Выберите вариант:",
            parse_mode="Markdown",
            reply_markup=kb.as_markup()
        )
        
    except Exception as e:
        logging.error(f"Ошибка при загрузке файла: {e}")
        await status_msg.edit_text("❌ Произошла ошибка при загрузке файла.")
        if task_dir.exists():
            shutil.rmtree(task_dir)
        if user_id in user_sessions:
            del user_sessions[user_id]

@router.message(F.document)
async def handle_docs(message: types.Message, bot: Bot, SHM_DIR: str, check_access, user_mgr, get_settings_keyboard):
    # ... (старый код для ZIP и других форматов – можно оставить без изменений)
    # Но если хотите, можно тоже адаптировать под выбор слайдов (для ZIP с PPTX)
    pass

@router.message(F.text.contains("http://") | F.text.contains("https://"))
async def handle_links(message: types.Message, bot: Bot, SHM_DIR: str, check_access, user_mgr, get_settings_keyboard):
    if not await check_access(message):
        return
    
    import converter_engine
    direct_url = converter_engine.convert_to_direct_download(message.text)
    
    user_id = message.from_user.id
    chat_id = message.chat.id
    
    task_id = generate_task_id(chat_id, user_id, message.message_id)
    task_dir = Path(SHM_DIR) / task_id
    task_dir.mkdir(exist_ok=True)
    
    ownership_file = task_dir / ".owner"
    ownership_file.write_text(f"{user_id}:{chat_id}")
    
    safe_file_name = "downloaded_presentation.pptx"
    download_path = task_dir / safe_file_name
    
    status_message = await message.reply("🌐 Скачивание ссылки в RAM...")
    
    try:
        if await download_file_by_url(direct_url, download_path, status_message):
            # Сохраняем сессию
            user_sessions[user_id] = {
                "task_id": task_id,
                "task_dir": task_dir,
                "file_path": download_path,
                "awaiting_selection": False,
                "ranges": []
            }
            
            kb = InlineKeyboardBuilder()
            kb.row(
                InlineKeyboardButton(text="📊 Все слайды", callback_data=f"slides_all:{task_id}"),
                InlineKeyboardButton(text="📝 Выбрать слайды", callback_data=f"slides_select:{task_id}")
            )
            
            await status_message.edit_text(
                "📄 **Файл успешно загружен по ссылке.**\n\n"
                "Какие слайды конвертировать?\n"
                "• Все слайды (по умолчанию)\n"
                "• Выборочные (например: 1-5, 10, 15-20)\n\n"
                "Выберите вариант:",
                parse_mode="Markdown",
                reply_markup=kb.as_markup()
            )
        else:
            await status_message.edit_text("❌ Не удалось скачать файл по ссылке. Проверьте доступность.")
            
    except Exception as e:
        await status_message.edit_text(f"❌ Ошибка ссылки: {e}")
        logging.error(f"Ошибка в handle_links: {e}", exc_info=True)
        if task_dir.exists():
            shutil.rmtree(task_dir)
        if user_id in user_sessions:
            del user_sessions[user_id]

# ==========================================
# 13. ОБРАБОТЧИК ТЕКСТОВЫХ СООБЩЕНИЙ (для ввода слайдов)
# ==========================================

@router.message(F.text & ~F.text.contains("http://") & ~F.text.contains("https://"))
async def handle_slides_or_text(message: types.Message, check_access, get_settings_keyboard):
    if not await check_access(message):
        return
    
    user_id = message.from_user.id
    session = user_sessions.get(user_id)
    
    # Если пользователь ожидает ввод слайдов
    if session and session.get("awaiting_selection"):
        ranges = parse_slides_ranges(message.text.strip())
        if not ranges:
            await message.reply(
                "❌ **Неверный формат.**\n\n"
                "Примеры правильного ввода:\n"
                "• `1, 3, 5, 7`\n"
                "• `4-12, 15, 20-30`\n"
                "• `1, 3-5, 10, 15-20`\n\n"
                "Попробуйте снова или нажмите кнопку 'Все слайды'."
            )
            return
        
        session["ranges"] = ranges
        session["awaiting_selection"] = False
        
        ranges_text = ", ".join([f"{r[0]}-{r[1]}" if r[0] != r[1] else str(r[0]) for r in ranges])
        kb = InlineKeyboardBuilder()
        kb.row(
            InlineKeyboardButton(text="✅ Конвертировать", callback_data=f"slides_convert:{session['task_id']}"),
            InlineKeyboardButton(text="✏️ Изменить", callback_data=f"slides_select:{session['task_id']}")
        )
        
        await message.reply(
            f"📊 **Вы выбрали слайды:** {ranges_text}\n\n"
            f"Будет создано {len(ranges)} архив(ов).\n"
            "Нажмите 'Конвертировать' для начала обработки.",
            parse_mode="Markdown",
            reply_markup=kb.as_markup()
        )
        return
    
    # Если не ждём ввод — показываем настройки
    await message.reply(
        "⚙️ **Параметры генерации слайдов:**\n\n"
        "Настройте качество и режим отправки PDF, затем загрузите презентацию.",
        reply_markup=get_settings_keyboard(user_id)
    )
