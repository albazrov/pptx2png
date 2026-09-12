# ==========================================
# handlers.py — ОБРАБОТЧИКИ (ФИНАЛЬНАЯ ВЕРСИЯ, ИСПРАВЛЕННАЯ)
# ==========================================

import os
import shutil
import logging
import secrets
import asyncio
import zipfile
import time
from pathlib import Path
from typing import Optional, Set, Dict, List, Tuple
from aiogram import Router, F, types, Bot
from aiogram.filters import CommandStart
from aiogram.types import InlineKeyboardButton, FSInputFile
from aiogram.utils.keyboard import InlineKeyboardBuilder

from utils import extract_text_from_pptx, check_spelling, download_file_by_url, core_pipeline
import converter_engine
from converter_engine import make_dark_mode


# ==========================================
# ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ
# ==========================================

sessions: Dict[str, dict] = {}
router = Router()
converter_semaphore = asyncio.Semaphore(2)


# ==========================================
# МЕНЕДЖЕР БЛОКИРОВОК ЗАДАЧ
# ==========================================

class TaskLockManager:
    """
    Менеджер блокировок для защиты от дублирующих операций.
    Предотвращает одновременный запуск конвертации и спеллера для одной задачи.
    """
    
    def __init__(self):
        self._locks: Dict[str, asyncio.Lock] = {}
        self._active: Set[str] = set()
        self._dict_lock = asyncio.Lock()
    
    async def acquire(self, task_id: str) -> bool:
        """
        Пытается захватить блокировку для задачи.
        Возвращает True если захват успешен, False если задача уже обрабатывается.
        """
        async with self._dict_lock:
            if task_id in self._active:
                return False
            
            if task_id not in self._locks:
                self._locks[task_id] = asyncio.Lock()
            
            lock = self._locks[task_id]
            if lock.locked():
                return False
            
            await lock.acquire()
            self._active.add(task_id)
            return True
    
    async def release(self, task_id: str):
        """Освобождает блокировку задачи."""
        async with self._dict_lock:
            self._active.discard(task_id)
            if task_id in self._locks:
                lock = self._locks[task_id]
                if lock.locked():
                    lock.release()
                self._locks.pop(task_id, None)


    async def is_active(self, task_id: str) -> bool:
        """Проверяет, активна ли задача (есть ли активная блокировка)."""
        async with self._dict_lock:
            return task_id in self._active


task_lock_manager = TaskLockManager()


# ==========================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ==========================================

def safe_filename(filename: str) -> str:
    import re
    safe_name = os.path.basename(filename)
    safe_name = re.sub(r'[^\w\s.-]', '', safe_name)
    safe_name = re.sub(r'\s+', ' ', safe_name).strip()
    if not safe_name:
        safe_name = f"file_{secrets.token_hex(4)}"
    if len(safe_name) > 100:
        name, ext = os.path.splitext(safe_name)
        safe_name = name[:90] + ext
    return safe_name


def validate_download_path(task_dir: Path, destination: Path) -> bool:
    try:
        return destination.resolve().parent == task_dir.resolve() or \
               destination.resolve().parent in task_dir.resolve().parents
    except Exception:
        return False


def generate_task_id(chat_id: int, user_id: int, message_id: int) -> str:
    return f"task_{chat_id}_{user_id}_{message_id}_{secrets.token_hex(8)}"


def parse_slides_ranges(input_text: str) -> List[Tuple[int, int]]:
    ranges = []
    parts = input_text.replace(" ", "").split(",")
    for part in parts:
        if not part:
            continue
        if "-" in part:
            try:
                start, end = map(int, part.split("-"))
                if start < 1 or end < 1:
                    return []
                if start > end:
                    start, end = end, start
                ranges.append((start, end))
            except ValueError:
                return []
        else:
            try:
                num = int(part)
                if num < 1:
                    return []
                ranges.append((num, num))
            except ValueError:
                return []
    return ranges


def reset_awaiting_for_user_chat(user_id: int, chat_id: int, exclude_task_id: Optional[str] = None):
    """Сбрасывает флаг awaiting_selection у всех сессий пользователя в чате, кроме указанной."""
    for tid, sess in sessions.items():
        if sess.get("user_id") == user_id and sess.get("chat_id") == chat_id:
            if exclude_task_id is None or tid != exclude_task_id:
                sess["awaiting_selection"] = False


def get_disabled_keyboard() -> InlineKeyboardBuilder:
    """Возвращает клавиатуру с заблокированной кнопкой."""
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="⏳ Конвертация...", callback_data="disabled_placeholder"))
    return kb


def touch_task(task_dir: Path):
    """
    Обновляет время модификации папки задачи.
    Используется для того, чтобы очистка не удаляла активные задачи.
    """
    if task_dir and task_dir.exists():
        try:
            os.utime(task_dir, None)
        except Exception as e:
            logging.error(f"Ошибка touch для {task_dir}: {e}")


# ==========================================
# НОРМАЛИЗАЦИЯ ДИАПАЗОНОВ
# ==========================================

def normalize_ranges(ranges: List[Tuple[int, int]]) -> List[Tuple[int, int]]:
    """
    Объединяет ТОЛЬКО перекрывающиеся диапазоны, но только если итоговый размер <= 1000.
    Соседние (1-3, 4-6) остаются отдельными.
    Точные дубликаты удаляются.
    """
    if not ranges:
        return []

    unique_ranges = list(dict.fromkeys(ranges))
    if not unique_ranges:
        return []
    
    sorted_ranges = sorted(unique_ranges, key=lambda r: r[0])
    
    merged = []
    start, end = sorted_ranges[0]
    idx = 0
    
    if end - start + 1 > 1000:
        logging.warning(f"Слишком большой диапазон {start}-{end}, игнорируем.")
        idx = 1
        while idx < len(sorted_ranges):
            s, e = sorted_ranges[idx]
            if e - s + 1 <= 1000:
                start, end = s, e
                break
            logging.warning(f"Слишком большой диапазон {s}-{e}, игнорируем.")
            idx += 1
        else:
            return []
    
    for next_start, next_end in sorted_ranges[idx+1:]:
        if next_end - next_start + 1 > 1000:
            logging.warning(f"Слишком большой диапазон {next_start}-{next_end}, игнорируем.")
            continue
        
        if next_start <= end:
            new_end = max(end, next_end)
            if new_end - start + 1 <= 1000:
                end = new_end
            else:
                merged.append((start, end))
                start, end = next_start, next_end
        else:
            merged.append((start, end))
            start, end = next_start, next_end
    
    merged.append((start, end))
    return merged


# ==========================================
# УДАЛЕНИЕ ПАПКИ ЗАДАЧИ
# ==========================================

def safe_delete_task_dir(task_dir: Path):
    """Безопасно удаляет папку задачи."""
    if task_dir and task_dir.exists():
        try:
            shutil.rmtree(task_dir)
            logging.info(f"🧹 Удалена папка задачи: {task_dir}")
        except Exception as e:
            logging.error(f"Ошибка удаления папки {task_dir}: {e}")


# ==========================================
# КОНТЕКСТНЫЙ МЕНЕДЖЕР ДЛЯ ЗАДАЧИ
# ==========================================

class TaskContext:
    def __init__(self, task_id: str, callback: types.CallbackQuery, SHM_DIR: str, operation: str = "conversion"):
        self.task_id = task_id
        self.callback = callback
        self.SHM_DIR = SHM_DIR
        self.operation = operation
        self.task_dir = None
        self.pptx_path = None
        self.session_data = None
        self.lock_acquired = False

    async def __aenter__(self):
        # ✅ 1. СНАЧАЛА проверяем сессию
        self.session_data = sessions.get(self.task_id)
        if not self.session_data:
            await self.callback.message.edit_text("❌ Сессия была удалена.")
            raise ValueError("Session not found")
        
        # ✅ 2. Проверяем файлы ДО захвата блокировки
        self.task_dir = Path(self.SHM_DIR) / self.task_id
        if not self.task_dir.exists():
            await self.callback.message.edit_text("❌ Папка задачи удалена.")
            raise FileNotFoundError("Task directory not found")
        
        self.pptx_path = self.session_data.get("file_path")
        if not self.pptx_path or not Path(self.pptx_path).exists():
            await self.callback.message.edit_text("❌ Файл презентации удален.")
            raise FileNotFoundError("Presentation file not found")
        
        # ✅ 3. ТОЛЬКО ПОСЛЕ ВСЕХ ПРОВЕРОК — захватываем блокировку
        if not await task_lock_manager.acquire(self.task_id):
            await self.callback.message.edit_text("⏳ Задача уже обрабатывается.")
            raise RuntimeError("Task already processing")
        self.lock_acquired = True
        
        # 4. Обновляем время активности
        touch_task(self.task_dir)
        
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        # Освобождаем блокировку
        if self.lock_acquired:
            await task_lock_manager.release(self.task_id)
        
        # Удаляем сессию в любом случае
        sessions.pop(self.task_id, None)
        
        # Удаляем папку задачи (безопасно)
        safe_delete_task_dir(self.task_dir)


# ==========================================
# ОСНОВНАЯ ФУНКЦИЯ КОНВЕРТАЦИИ
# ==========================================

async def run_conversion(
    callback: types.CallbackQuery,
    task_id: str,
    SHM_DIR: str,
    user_mgr,
    get_settings_keyboard,
    all_slides: bool = True,
    ranges: List[Tuple[int, int]] = None
):
    # --- 1. Первая валидация (до захвата ресурсов) ---
    session = sessions.get(task_id)
    if not session:
        await callback.message.edit_text("❌ Сессия истекла.")
        await callback.answer("❌ Сессия истекла.", show_alert=True)
        return
    
    if callback.from_user.id != session["user_id"] or callback.message.chat.id != session["chat_id"]:
        await callback.message.edit_text("❌ У вас нет доступа к этой задаче.")
        await callback.answer("❌ У вас нет доступа к этой задаче.", show_alert=True)
        return

    # --- 2. Ожидание слота (семафор) и контекст с блокировкой ---
    async with converter_semaphore:
        try:
            async with TaskContext(task_id, callback, SHM_DIR, "conversion") as ctx:
                
                cfg = user_mgr.get_user_config(callback.from_user.id)
                chat_id = callback.message.chat.id
                user_id = callback.from_user.id
                pptx_path = ctx.pptx_path

                # Обновляем время активности перед началом длительных операций
                touch_task(ctx.task_dir)

                if all_slides:
                    expected_zip, final_pdf_path = await core_pipeline(pptx_path, callback.message, user_id, user_mgr)
                    
                    if expected_zip and expected_zip.exists():
                        if expected_zip.stat().st_size > 45 * 1024 * 1024:
                            await callback.message.edit_text("⚠️ **Архив слишком большой (>45 МБ).**")
                            return
                        
                        await callback.message.edit_text("📤 Отправляю готовые файлы...")
                        await callback.bot.send_document(chat_id=chat_id, document=FSInputFile(expected_zip), caption="📦 ZIP со всеми слайдами готов!")
                        
                        if final_pdf_path and final_pdf_path.exists():
                            await callback.bot.send_document(chat_id=chat_id, document=FSInputFile(final_pdf_path), caption="📄 PDF готов!")
                            
                        await callback.message.delete()
                    else:
                        await callback.message.edit_text("❌ Ошибка конвертации всех слайдов.")

                elif ranges:
                    final_ranges = normalize_ranges(ranges)
                    
                    if not final_ranges:
                        await callback.message.edit_text("❌ Нет допустимых диапазонов для конвертации.")
                        return
                    
                    temp_png_dir = ctx.task_dir / "temp_pngs"
                    temp_png_dir.mkdir(exist_ok=True)
                    
                    # Обновляем время перед конвертацией PNG
                    touch_task(ctx.task_dir)

                    all_pngs = await convert_all_pngs(pptx_path, temp_png_dir, cfg["quality"])
                    if not all_pngs:
                        await callback.message.edit_text("❌ Не удалось конвертировать слайды в PNG.")
                        return

                    total_slides = len(all_pngs)
                    archives = []

                    for idx, (start, end) in enumerate(final_ranges):
                        if start > total_slides:
                            await callback.message.edit_text(f"❌ Слайд {start} не существует (всего {total_slides}).")
                            return
                        if end > total_slides:
                            end = total_slides

                        selected = []
                        for i in range(start - 1, end):
                            if i < len(all_pngs):
                                selected.append(all_pngs[i])
                        if not selected:
                            continue

                        range_name = f"slides_{start}-{end}" if start != end else f"slide_{start}"
                        zip_path = ctx.task_dir / f"{pptx_path.stem}_part{idx + 1}_{range_name}.zip"
                        
                        create_zip_stream(selected, zip_path)
                        
                        # Обновляем время после создания каждого архива
                        touch_task(ctx.task_dir)

                        if zip_path.stat().st_size > 45 * 1024 * 1024:
                            zip_path.unlink()
                            await callback.message.edit_text(
                                f"⚠️ **Архив для диапазона {start}-{end} слишком большой (>45 МБ).**"
                            )
                            return
                        archives.append(zip_path)

                    for png_path in all_pngs:
                        if png_path.exists():
                            png_path.unlink()
                    if temp_png_dir.exists():
                        shutil.rmtree(temp_png_dir)

                    if archives:
                        await callback.message.edit_text(f"📤 Отправляю {len(archives)} архив(ов)...")
                        for zip_path in archives:
                            if zip_path.exists():
                                await callback.bot.send_document(
                                    chat_id=chat_id,
                                    document=FSInputFile(zip_path),
                                    caption=f"📦 {zip_path.name}"
                                )
                                zip_path.unlink()
                                
                                # Обновляем время после отправки каждого архива
                                touch_task(ctx.task_dir)
                        await callback.message.delete()
                        await callback.bot.send_message(
                            chat_id=chat_id,
                            text="⚙️ **Настройки для следующей презентации:**",
                            reply_markup=get_settings_keyboard(user_id)
                        )
                    else:
                        await callback.message.edit_text("❌ Ошибка создания архивов.")

        except RuntimeError as e:
            if "already processing" in str(e):
                logging.warning(f"Попытка повторного запуска конвертации для задачи {task_id}")
                await callback.answer("⏳ Задача уже обрабатывается, пожалуйста, подождите...", show_alert=True)
            else:
                logging.error(f"RuntimeError в run_conversion: {e}")
                await callback.message.edit_text(f"❌ Ошибка: {str(e)[:100]}")
        except ValueError as e:
            logging.error(f"Ошибка валидации в run_conversion: {e}")
            await callback.message.edit_text(f"❌ Ошибка данных: {str(e)[:100]}")
            await callback.answer("❌ Ошибка данных, попробуйте заново.", show_alert=True)
        except FileNotFoundError as e:
            logging.error(f"Файл не найден: {e}")
            await callback.message.edit_text("❌ Презентация была удалена или повреждена.")
            await callback.answer("❌ Презентация не найдена.", show_alert=True)
        except Exception as e:
            logging.error(f"Неожиданная ошибка в run_conversion: {e}", exc_info=True)
            try:
                await callback.message.edit_text(f"❌ Произошла ошибка: {str(e)[:100]}")
                await callback.answer("❌ Произошла ошибка, попробуйте заново.", show_alert=True)
            except Exception:
                pass


# ==========================================
# КОНВЕРТАЦИЯ В PNG
# ==========================================

async def convert_all_pngs(pptx_path: Path, output_dir: Path, quality: str) -> List[Path]:
    def _sync_convert():
        if pptx_path.suffix.lower() == '.ppt':
            pptx_converted = converter_engine.ppt_to_pptx_crossplatform(pptx_path, output_dir)
        else:
            pptx_converted = pptx_path
            
        temp_dark_pptx = output_dir / f"temp_dark_{pptx_converted.name}"
        make_dark_mode(pptx_converted, temp_dark_pptx)
        
        pdf_path = converter_engine.pptx_to_pdf_crossplatform(temp_dark_pptx, output_dir)
        total_slides, png_paths = converter_engine.pdf_to_png_fast(pdf_path, output_dir, quality)
        
        if pdf_path.exists():
            pdf_path.unlink()
        if temp_dark_pptx.exists():
            temp_dark_pptx.unlink()
        if pptx_converted != pptx_path and pptx_converted.exists():
            pptx_converted.unlink()
            
        return png_paths
    return await asyncio.to_thread(_sync_convert)


# ==========================================
# ПОТОКОВОЕ СОЗДАНИЕ ZIP
# ==========================================

def create_zip_stream(file_paths: List[Path], output_path: Path) -> Path:
    with zipfile.ZipFile(output_path, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for fpath in file_paths:
            if fpath.exists():
                zf.write(fpath, arcname=fpath.name)
    return output_path
# ==========================================
# ВСПОМОГАТЕЛЬНАЯ ФУНКЦИЯ ДЛЯ ПРИВЕТСТВИЯ
# ==========================================

async def send_welcome(message: types.Message, get_settings_keyboard):
    """Отправляет приветственное сообщение с настройками."""
    await message.reply(
        "👋 Привет!\n\n"
        "Для начала работы загрузите презентацию в формате **.pptx**, **.ppt** или **.zip**.\n"
        "Также можно отправить google ссылку на файл (доступ на чтением всем).\n\n"
        "⚙️ Настройки качества и PDF:",
        reply_markup=get_settings_keyboard(message.from_user.id)
    )

# ==========================================
# ХЕНДЛЕРЫ (ПОРЯДОК ВАЖЕН!)
# ==========================================

# ==========================================
# 1. КОМАНДА СТАРТ (ДОЛЖНА БЫТЬ ПЕРВОЙ)
# ==========================================

@router.message(CommandStart())
async def cmd_start(message: types.Message, check_access, get_settings_keyboard):
    if not await check_access(message):
        return
    await send_welcome(message, get_settings_keyboard)

# ==========================================
# 2. ОБРАБОТЧИК ВЫБОРА СЛАЙДОВ (callback)
# ==========================================

@router.callback_query(F.data.startswith("slides_all:"))
async def handle_all_slides(callback: types.CallbackQuery, bot: Bot, SHM_DIR: str, user_mgr, check_access_by_user, get_settings_keyboard):
    if not await check_access_by_user(callback.from_user, bot):
        await callback.answer("❌ Доступ запрещен.", show_alert=True)
        return
    task_id = callback.data.split(":")[-1]
    if task_id not in sessions:
        await callback.answer("❌ Сессия истекла.", show_alert=True)
        return
    
    # Блокируем кнопку
    try:
        await callback.message.edit_reply_markup(reply_markup=get_disabled_keyboard().as_markup())
    except Exception:
        pass
    
    await callback.answer("⏳ Начинаю конвертацию...")
    await callback.message.edit_text("⚙️ Запускаю конвертацию всех слайдов...")
    await run_conversion(callback, task_id, SHM_DIR, user_mgr, get_settings_keyboard, all_slides=True)


@router.callback_query(F.data.startswith("slides_select:"))
async def handle_select_slides(callback: types.CallbackQuery, bot: Bot):
    task_id = callback.data.split(":")[-1]
    if task_id not in sessions:
        await callback.answer("❌ Сессия истекла.", show_alert=True)
        return
    
    session = sessions[task_id]
    
    # ========== ИСПРАВЛЕНИЕ: проверка владельца ==========
    if callback.from_user.id != session.get("user_id") or callback.message.chat.id != session.get("chat_id"):
        await callback.answer("❌ У вас нет доступа к этой задаче.", show_alert=True)
        return
    # ====================================================
    
    # Проверка существования папки
    task_dir = Path(session.get("task_dir", ""))
    if not task_dir.exists():
        sessions.pop(task_id, None)
        await callback.answer("❌ Данные задачи устарели.", show_alert=True)
        await callback.message.edit_text("❌ Данные задачи устарели. Пожалуйста, загрузите презентацию заново.")
        return
    
    # Обновляем время активности
    touch_task(task_dir)
    
    reset_awaiting_for_user_chat(session["user_id"], session["chat_id"], exclude_task_id=task_id)
    
    sessions[task_id]["awaiting_selection"] = True
    await callback.message.edit_text(
        "📝 **Введите номера слайдов для конвертации.** \n\n"
        "Формат ввода:\n"
        "• Отдельные номера: `1, 3, 5, 7` \n"
        "• Диапазоны: `4-12, 15, 20-30` \n"
        "• Смешанный: `1, 3-5, 10, 15-20` \n\n"
        "Если укажете несколько диапазонов, каждый будет упакован в отдельный архив.", 
        parse_mode="Markdown"
    )
    await callback.answer()


@router.callback_query(F.data.startswith("slides_convert:"))
async def handle_convert_selected(callback: types.CallbackQuery, bot: Bot, SHM_DIR: str, user_mgr, check_access_by_user, get_settings_keyboard):
    if not await check_access_by_user(callback.from_user, bot):
        await callback.answer("❌ Доступ запрещен.", show_alert=True)
        return
    task_id = callback.data.split(":")[-1]
    session = sessions.get(task_id)
    if not session:
        await callback.answer("❌ Сессия истекла.", show_alert=True)
        return
    
    # Проверка существования папки
    task_dir = Path(session.get("task_dir", ""))
    if not task_dir.exists():
        sessions.pop(task_id, None)
        await callback.answer("❌ Данные задачи устарели.", show_alert=True)
        await callback.message.edit_text("❌ Данные задачи устарели. Пожалуйста, загрузите презентацию заново.")
        return
    
    ranges = session.get("ranges")
    if not ranges:
        await callback.answer("❌ Не выбраны слайды.", show_alert=True)
        return
    
    # Обновляем время активности
    touch_task(task_dir)
    
    # Блокируем кнопку
    try:
        await callback.message.edit_reply_markup(reply_markup=get_disabled_keyboard().as_markup())
    except Exception:
        pass
    
    await callback.answer("⏳ Начинаю конвертацию...")
    await callback.message.edit_text(f"⚙️ Запускаю конвертацию {len(ranges)} диапазон(ов)...")
    await run_conversion(callback, task_id, SHM_DIR, user_mgr, get_settings_keyboard, all_slides=False, ranges=ranges)


# ==========================================
# 3. ОБРАБОТЧИК НАЖАТИЯ НА ЗАБЛОКИРОВАННУЮ КНОПКУ
# ==========================================

@router.callback_query(F.data == "disabled_placeholder")
async def handle_disabled_button(callback: types.CallbackQuery):
    """Обработчик нажатия на заблокированную кнопку."""
    await callback.answer("⏳ Идёт обработка, пожалуйста, подождите...", show_alert=True)


# ==========================================
# 4. ОБРАБОТЧИК ТЕКСТА (ИСКЛЮЧАЕТ КОМАНДЫ)
# ==========================================

@router.message(F.text & ~F.text.contains("http://") & ~F.text.contains("https://") & ~F.text.startswith("/"))
async def handle_text_input(message: types.Message, check_access, get_settings_keyboard):
    if not await check_access(message):
        return
    user_id = message.from_user.id
    target_chat_id = message.chat.id

    active_session = None
    active_task_id = None
    
    for tid, sess in sessions.items():
        if sess.get("user_id") == user_id and \
           sess.get("chat_id") == target_chat_id and \
           sess.get("awaiting_selection"):
            active_session = sess
            active_task_id = tid
            break

    if not active_session:
        # Вместо ошибки отправляем приветственное сообщение,
        # чтобы пользователь понял, что нужно загрузить файл.
        await send_welcome(message, get_settings_keyboard)

        return

    ranges = parse_slides_ranges(message.text.strip())
    if not ranges:
        await message.reply(
            "❌ **Неверный формат.**\n\nПримеры: `1, 3, 5, 7` или `4-12, 15, 20-30`"
        )
        return

    # Обновляем время активности
    task_dir = Path(active_session.get("task_dir", ""))
    touch_task(task_dir)

    active_session["ranges"] = ranges
    active_session["awaiting_selection"] = False

    ranges_text = ", ".join([f"{r[0]}-{r[1]}" if r[0] != r[1] else str(r[0]) for r in ranges])
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="✅ Конвертировать", callback_data=f"slides_convert:{active_task_id}"),
        InlineKeyboardButton(text="✏️ Изменить", callback_data=f"slides_select:{active_task_id}")
    )
    await message.reply(
        f"📊 **Вы выбрали:** {ranges_text}\n\n{len(ranges)} архив(ов).\nНажмите 'Конвертировать'.",
        parse_mode="Markdown", reply_markup=kb.as_markup()
    )


# ==========================================
# 5. ОБРАБОТЧИК СПЕЛЛЕРА (С БЛОКИРОВКОЙ)
# ==========================================

@router.callback_query(F.data.startswith("chk_spell:"))
async def callback_run_speller(callback: types.CallbackQuery, bot: Bot, SHM_DIR: str, check_access_by_user):
    if not await check_access_by_user(callback.from_user, bot):
        await callback.answer("❌ Доступ запрещен.", show_alert=True)
        return
    task_id = callback.data.split(":")[-1]
    
    # ✅ Захватываем блокировку для спеллера
    if not await task_lock_manager.acquire(task_id):
        await callback.answer("⏳ Задача уже обрабатывается.", show_alert=True)
        return
    
    try:
        task_dir, pptx_path = await _validate_task_ownership(callback, task_id, SHM_DIR)
        if not task_dir or not pptx_path:
            return

        # Обновляем время активности
        touch_task(task_dir)

        disabled_kb = InlineKeyboardBuilder()
        disabled_kb.row(InlineKeyboardButton(text="⏳ Обработка...", callback_data=f"disabled_{task_id}"))
        await callback.message.edit_reply_markup(reply_markup=disabled_kb.as_markup())
        await callback.message.edit_text("🔍 Извлекаю текст и отправляю в Яндекс.Спеллер...")

        from utils import extract_text_from_pptx, check_spelling
        extract_success, slides_text = await asyncio.to_thread(extract_text_from_pptx, str(pptx_path))

        if not extract_success:
            await callback.message.edit_text(
                "❌ **Не удалось извлечь текст из презентации.**\n\n"
                "Вы можете продолжить конвертацию без проверки орфографии:",
                parse_mode="Markdown"
            )
            kb = InlineKeyboardBuilder()
            kb.row(InlineKeyboardButton(text="⚙️ Конвертировать", callback_data=f"chk_conv:{task_id}"))
            await callback.message.edit_reply_markup(reply_markup=kb.as_markup())
            await callback.answer()
            return

        check_success, spelling_result = await check_spelling(slides_text)

        kb = InlineKeyboardBuilder()
        kb.row(InlineKeyboardButton(text="⚙️ Всё равно конвертировать", callback_data=f"chk_conv:{task_id}"))

        if not check_success:
            await callback.message.edit_text(
                f"{spelling_result}\n\nВы можете продолжить конвертацию без проверки орфографии:",
                parse_mode="HTML", reply_markup=kb.as_markup()
            )
        else:
            await callback.message.edit_text(spelling_result, parse_mode="HTML", reply_markup=kb.as_markup())

        await callback.answer()

    except Exception as e:
        logging.error(f"Ошибка в callback_run_speller: {e}", exc_info=True)
        await callback.answer("❌ Произошла ошибка при проверке.", show_alert=True)
    finally:
        # ✅ Освобождаем блокировку после завершения
        await task_lock_manager.release(task_id)


# ==========================================
# 6. ОБРАБОТЧИК СТАРОЙ КОНВЕРТАЦИИ
# ==========================================

@router.callback_query(F.data.startswith("chk_conv:"))
async def callback_run_conversion(callback: types.CallbackQuery, bot: Bot, SHM_DIR: str, user_mgr, check_access_by_user, get_settings_keyboard):
    if not await check_access_by_user(callback.from_user, bot):
        await callback.answer("❌ Доступ запрещен.", show_alert=True)
        return
    task_id = callback.data.split(":")[-1]
    if task_id not in sessions:
        await callback.answer("❌ Сессия истекла.", show_alert=True)
        return
    
    # Обновляем время активности
    session = sessions[task_id]
    task_dir = Path(session.get("task_dir", ""))
    touch_task(task_dir)
    
    # Блокируем кнопку
    try:
        await callback.message.edit_reply_markup(reply_markup=get_disabled_keyboard().as_markup())
    except Exception:
        pass
    
    await callback.answer("⏳ Начинаю конвертацию...")
    await callback.message.edit_text("⚙️ Запускаю конвертацию...")
    await run_conversion(callback, task_id, SHM_DIR, user_mgr, get_settings_keyboard, all_slides=True)


# ==========================================
# 7. ПРОВЕРКА ВЛАДЕЛЬЦА ЗАДАЧИ
# ==========================================

async def _validate_task_ownership(callback: types.CallbackQuery, task_id: str, SHM_DIR: str) -> tuple:
    task_dir = Path(SHM_DIR) / task_id
    ownership_file = task_dir / ".owner"
    if not task_dir.exists():
        await callback.answer("❌ Срок действия сессии истек.", show_alert=True)
        return None, None
    if not ownership_file.exists():
        await callback.answer("❌ Данные задачи повреждены.", show_alert=True)
        return None, None
    try:
        owner_data = ownership_file.read_text().strip()
        owner_user_id, owner_chat_id = map(int, owner_data.split(":"))
    except Exception:
        await callback.answer("❌ Ошибка чтения данных задачи.", show_alert=True)
        return None, None
    if callback.from_user.id != owner_user_id:
        await callback.answer("❌ Эта задача принадлежит другому пользователю.", show_alert=True)
        return None, None
    if callback.message.chat.id != owner_chat_id:
        await callback.answer("❌ Эта задача создана в другом чате.", show_alert=True)
        return None, None
    pptx_path = next(task_dir.glob("*.pptx"), None)
    if not pptx_path:
        await callback.answer("❌ Файл презентации не найден.", show_alert=True)
        return None, None
    return task_dir, pptx_path


# ==========================================
# 8. АДМИНСКИЕ ХЕНДЛЕРЫ
# ==========================================

@router.callback_query(F.data.startswith("adm_"))
async def handle_admin_decision(callback: types.CallbackQuery, user_mgr, bot: Bot, ADMIN_ID: int):
    if callback.from_user.id != ADMIN_ID:
        return
    data = callback.data.split("_")
    action, target_id = data[1], int(data[2])
    if action == "allow":
        user_mgr.save_allowed_user(target_id)
        await callback.message.edit_text(f"✅ Доступ для `{target_id}` одобрен.")
        try:
            await bot.send_message(target_id, "🎉 Доступ одобрен! Нажмите /start.")
        except Exception:
            pass
    elif action == "deny":
        await callback.message.edit_text(f"❌ Запрос `{target_id}` отклонен.")
        try:
            await bot.send_message(target_id, "❌ Доступ отклонен.")
        except Exception:
            pass
    await callback.answer()


# ==========================================
# 9. НАСТРОЙКИ КАЧЕСТВА И PDF
# ==========================================

@router.callback_query(F.data.startswith("set_q_"))
async def handle_quality_settings(callback: types.CallbackQuery, user_mgr, get_settings_keyboard, check_access_by_user, bot: Bot):
    if not await check_access_by_user(callback.from_user, bot):
        await callback.answer("❌ Доступ запрещен.", show_alert=True)
        return
    user_id = callback.from_user.id
    new_quality = callback.data.replace("set_q_", "")
    user_mgr.update_user_config(user_id, "quality", new_quality)
    try:
        await callback.message.edit_reply_markup(reply_markup=get_settings_keyboard(user_id))
        await callback.answer(f"Качество обновлено: {new_quality.upper()}")
    except Exception as e:
        logging.error(f"Error updating quality keyboard: {e}")
        await callback.answer("❌ Ошибка обновления качества", show_alert=True)


@router.callback_query(F.data == "toggle_pdf")
async def handle_toggle_pdf(callback: types.CallbackQuery, user_mgr, get_settings_keyboard, check_access_by_user, bot: Bot):
    if not await check_access_by_user(callback.from_user, bot):
        await callback.answer("❌ Доступ запрещен.", show_alert=True)
        return
    user_id = callback.from_user.id
    current_config = user_mgr.get_user_config(user_id)
    new_pdf_status = not current_config.get("keep_pdf", False)
    user_mgr.update_user_config(user_id, "keep_pdf", new_pdf_status)
    try:
        await callback.message.edit_reply_markup(reply_markup=get_settings_keyboard(user_id))
        status_text = "Да (ZIP + PDF)" if new_pdf_status else "Нет (Только ZIP)"
        await callback.answer(f"PDF: {status_text}")
    except Exception as e:
        logging.error(f"Error toggling PDF keyboard: {e}")
        await callback.answer("❌ Ошибка обновления PDF", show_alert=True)


# ==========================================
# 10. ОБРАБОТЧИКИ ФАЙЛОВ
# ==========================================

@router.message(F.document.file_name.lower().endswith(('.pptx', '.ppt')))
async def handle_pptx_document(message: types.Message, bot: Bot, SHM_DIR: str, check_access):
    if not await check_access(message):
        return
    document = message.document
    user_id = message.from_user.id
    chat_id = message.chat.id

    safe_name = safe_filename(document.file_name)
    if not safe_name.lower().endswith(('.pptx', '.ppt')):
        await message.reply("❌ Неверный формат.")
        return

    task_id = generate_task_id(chat_id, user_id, message.message_id)
    task_dir = Path(SHM_DIR) / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / ".owner").write_text(f"{user_id}:{chat_id}")

    file_path = task_dir / safe_name
    if not validate_download_path(task_dir, file_path):
        await message.reply("❌ Ошибка безопасности.")
        return

    status_msg = await message.reply("⏳ Скачиваю презентацию...")
    success = False
    
    try:
        file_info = await bot.get_file(document.file_id)
        await bot.download_file(file_info.file_path, destination=file_path)

        reset_awaiting_for_user_chat(user_id, chat_id)
        sessions[task_id] = {
            "user_id": user_id,
            "chat_id": chat_id,
            "task_dir": task_dir,
            "file_path": file_path,
            "awaiting_selection": True,
            "ranges": []
        }
        kb = InlineKeyboardBuilder()
        kb.row(
            InlineKeyboardButton(text="📊 Все слайды", callback_data=f"slides_all:{task_id}"),
            InlineKeyboardButton(text="📝 Выбрать слайды", callback_data=f"slides_select:{task_id}")
        )
        await status_msg.edit_text(
            f"📄 **Файл '{safe_name}' загружен.**\n\n"
            "Вы можете сразу ввести номера слайдов в чат или выбрать вариант ниже:",
            parse_mode="Markdown", reply_markup=kb.as_markup()
        )
        success = True
        # Обновляем время активности
        touch_task(task_dir)
        
    except Exception as e:
        logging.error(f"Ошибка загрузки: {e}")
        try:
            await status_msg.edit_text("❌ Ошибка загрузки.")
        except Exception:
            pass
        
        if task_id in sessions:
            sessions.pop(task_id, None)
        safe_delete_task_dir(task_dir)
        raise
    finally:
        if not success and task_id in sessions:
            sessions.pop(task_id, None)
        if not success:
            safe_delete_task_dir(task_dir)


@router.message(F.document)
async def handle_docs(message: types.Message, bot: Bot, SHM_DIR: str, check_access, user_mgr, get_settings_keyboard):
    if not await check_access(message):
        return
    safe_name = safe_filename(message.document.file_name)
    ext = Path(safe_name).suffix.lower()
    if ext not in ['.zip', '.pptx', '.ppt']:
        await message.reply("❌ Поддерживаются только PPTX, PPT и ZIP.")
        return

    user_id = message.from_user.id
    chat_id = message.chat.id
    task_id = generate_task_id(chat_id, user_id, message.message_id)
    task_dir = Path(SHM_DIR) / task_id
    task_dir.mkdir(exist_ok=True)
    (task_dir / ".owner").write_text(f"{user_id}:{chat_id}")

    file_path = task_dir / safe_name
    if not validate_download_path(task_dir, file_path):
        await message.reply("❌ Ошибка безопасности.")
        return

    status_msg = await message.reply("📥 Загрузка...")
    success = False
    
    try:
        file_info = await bot.get_file(message.document.file_id)
        await bot.download_file(file_info.file_path, destination=file_path)

        if not file_path.exists() or file_path.stat().st_size == 0:
            await status_msg.edit_text("❌ Пустой файл.")
            return  # finally удалит папку

        if ext == '.zip':
            pptx_path = converter_engine.extract_zip_if_needed(file_path, task_dir)
            if not pptx_path:
                await status_msg.edit_text("❌ В ZIP нет презентации.")
                return  # finally удалит папку
            file_path = pptx_path

        reset_awaiting_for_user_chat(user_id, chat_id)
        sessions[task_id] = {
            "user_id": user_id,
            "chat_id": chat_id,
            "task_dir": task_dir,
            "file_path": file_path,
            "awaiting_selection": True,
            "ranges": []
        }
        kb = InlineKeyboardBuilder()
        kb.row(
            InlineKeyboardButton(text="📊 Все слайды", callback_data=f"slides_all:{task_id}"),
            InlineKeyboardButton(text="📝 Выбрать слайды", callback_data=f"slides_select:{task_id}")
        )
        await status_msg.edit_text(
            f"📄 **Файл '{safe_name}' загружен.**\n\n"
            "Вы можете сразу ввести номера слайдов в чат или выбрать вариант ниже:",
            parse_mode="Markdown", reply_markup=kb.as_markup()
        )
        success = True
        # Обновляем время активности
        touch_task(task_dir)
        
    except Exception as e:
        logging.error(f"Ошибка загрузки ZIP: {e}")
        try:
            await status_msg.edit_text("❌ Ошибка обработки архива.")
        except Exception:
            pass
        
        if task_id in sessions:
            sessions.pop(task_id, None)
        safe_delete_task_dir(task_dir)
        raise
    finally:
        if not success and task_id in sessions:
            sessions.pop(task_id, None)
        if not success:
            safe_delete_task_dir(task_dir)


# ==========================================
# 11. ОБРАБОТЧИК ССЫЛОК
# ==========================================

@router.message(F.text.contains("http://") | F.text.contains("https://"))
async def handle_links(message: types.Message, bot: Bot, SHM_DIR: str, check_access):
    if not await check_access(message):
        return
    
    url = converter_engine.convert_to_direct_download(message.text.strip())
    user_id = message.from_user.id
    chat_id = message.chat.id
    task_id = generate_task_id(chat_id, user_id, message.message_id)
    task_dir = Path(SHM_DIR) / task_id
    task_dir.mkdir(exist_ok=True)
    (task_dir / ".owner").write_text(f"{user_id}:{chat_id}")

    file_path = task_dir / "downloaded_presentation.pptx"
    status_msg = await message.reply("🌐 Скачивание ссылки...")
    success = False
    
    try:
        # Проверяем, является ли ссылка Яндекс.Диском
        is_yandex_disk = "disk.yandex" in url or "yadi.sk" in url
    
        if is_yandex_disk:
            # Используем функцию для Яндекс.Диска
            download_success = await download_yandex_disk(url, file_path)
        else:
            #  существующая логика (Google Docs и т.д.)
            download_success = await download_file_by_url(url, file_path, status_msg)
        if not download_success:
            await status_msg.edit_text("❌ Не удалось скачать файл по ссылке.")
            return  # finally удалит папку

        reset_awaiting_for_user_chat(user_id, chat_id)
        sessions[task_id] = {
            "user_id": user_id,
            "chat_id": chat_id,
            "task_dir": task_dir,
            "file_path": file_path,
            "awaiting_selection": True,
            "ranges": []
        }
        
        kb = InlineKeyboardBuilder()
        kb.row(
            InlineKeyboardButton(text="📊 Все слайды", callback_data=f"slides_all:{task_id}"),
            InlineKeyboardButton(text="📝 Выбрать слайды", callback_data=f"slides_select:{task_id}")
        )
        await status_msg.edit_text(
            "📄 **Файл загружен по ссылке.**\n\n"
            "Вы можете сразу ввести номера слайдов в чат или выбрать вариант ниже:",
            reply_markup=kb.as_markup()
        )
        success = True
        # Обновляем время активности
        touch_task(task_dir)
        
    except Exception as e:
        logging.error(f"Ошибка в handle_links: {e}")
        try:
            await status_msg.edit_text(f"❌ Ошибка: {e}")
        except Exception:
            pass
        
        if task_id in sessions:
            sessions.pop(task_id, None)
        safe_delete_task_dir(task_dir)
        raise
    finally:
        if not success and task_id in sessions:
            sessions.pop(task_id, None)
        if not success:
            safe_delete_task_dir(task_dir)
