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

from utils import extract_text_from_pptx, check_spelling, download_file_by_url, core_pipeline
import converter_engine
from converter_engine import make_dark_mode

# ==========================================
# ГЛОБАЛЬНЫЙ МЕНЕДЖЕР БЛОКИРОВОК (ОПТИМИЗИРОВАННЫЙ)
# ==========================================

class TaskLockManager:
    def __init__(self):
        self._locks: Dict[str, asyncio.Lock] = {}
        self._states: Dict[str, str] = {}
        self._active_operations: Dict[str, Set[str]] = {}
        self._last_activity: Dict[str, float] = {}
        self._dict_lock = asyncio.Lock()

    async def acquire(self, task_id: str, operation: str) -> bool:
        async with self._dict_lock:
            if task_id not in self._locks:
                self._locks[task_id] = asyncio.Lock()
            current_state = self._states.get(task_id, "idle")
            if current_state in ("processing", "completed"):
                return False
            lock = self._locks[task_id]
            acquired = lock.locked() or await asyncio.shield(lock.acquire())
            if acquired:
                self._states[task_id] = "processing"
                if task_id not in self._active_operations:
                    self._active_operations[task_id] = set()
                self._active_operations[task_id].add(operation)
                self._last_activity[task_id] = asyncio.get_event_loop().time()
                return True
            return False

    async def release(self, task_id: str, operation: str):
        """Освобождает блокировку синхронно, без создания лишних задач."""
        async with self._dict_lock:
            if task_id not in self._locks:
                return
            if task_id in self._active_operations:
                self._active_operations[task_id].discard(operation)
                if not self._active_operations[task_id]:
                    self._locks.pop(task_id, None)
                    self._states.pop(task_id, None)
                    self._active_operations.pop(task_id, None)
                    self._last_activity.pop(task_id, None)
                    return
            self._last_activity[task_id] = asyncio.get_event_loop().time()
            if task_id in self._locks:
                lock = self._locks[task_id]
                if lock.locked():
                    lock.release()

    async def cleanup_loop(self, interval: int = 300, max_age: int = 3600):
        while True:
            await asyncio.sleep(interval)
            async with self._dict_lock:
                current_time = asyncio.get_event_loop().time()
                expired = [tid for tid, t in self._last_activity.items() if current_time - t > max_age]
                for tid in expired:
                    self._locks.pop(tid, None)
                    self._states.pop(tid, None)
                    self._active_operations.pop(tid, None)
                    self._last_activity.pop(tid, None)
                    logging.debug(f"🧹 Очищена устаревшая запись: {tid}")


task_lock_manager = TaskLockManager()
sessions: Dict[str, dict] = {}
router = Router()

# ==========================================
# ГЛОБАЛЬНЫЙ СЕМАФОР ДЛЯ ОГРАНИЧЕНИЯ КОНВЕРТАЦИЙ
# ==========================================
converter_semaphore = asyncio.Semaphore(2)


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
        return destination.resolve().parent == task_dir.resolve() or destination.resolve().parent in task_dir.resolve().parents
    except Exception:
        return False


def generate_task_id(chat_id: int, user_id: int, message_id: int) -> str:
    return f"task_{chat_id}_{user_id}_{message_id}_{secrets.token_hex(2)}"


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


# ==========================================
# ОПТИМИЗИРОВАННАЯ КОНВЕРТАЦИЯ В PNG
# ==========================================

async def convert_all_pngs(pptx_path: Path, output_dir: Path, quality: str) -> List[Path]:
    """Конвертирует все слайды в PNG с применением тёмной темы. Поддерживает .ppt."""

    def _sync_convert():
        # Если файл .ppt, конвертируем в .pptx
        if pptx_path.suffix.lower() == '.ppt':
            pptx_converted = converter_engine.ppt_to_pptx_crossplatform(pptx_path, output_dir)
        else:
            pptx_converted = pptx_path

        # Тёмная тема
        temp_dark_pptx = output_dir / f"temp_dark_{pptx_converted.name}"
        make_dark_mode(pptx_converted, temp_dark_pptx)

        # PDF
        pdf_path = converter_engine.pptx_to_pdf_crossplatform(temp_dark_pptx, output_dir)

        # PNG
        total_slides, png_paths = converter_engine.pdf_to_png_fast(pdf_path, output_dir, quality)

        # Очистка
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

def create_zip_stream(file_paths: List[Path], output_path: Path, chunk_size: int = 50) -> Path:
    """Создаёт ZIP-архив в потоковом режиме, удаляя файлы по мере упаковки."""
    with zipfile.ZipFile(output_path, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for i, fpath in enumerate(file_paths):
            zf.write(fpath, arcname=fpath.name)
            # Удаляем каждый файл после упаковки, чтобы экономить RAM
            if fpath.exists():
                fpath.unlink()
            # Периодически сбрасываем буфер
            if i % chunk_size == 0:
                zf.close()
                zf = zipfile.ZipFile(output_path, 'a', zipfile.ZIP_DEFLATED, compresslevel=6)
    return output_path


# ==========================================
# ОСНОВНАЯ ФУНКЦИЯ КОНВЕРТАЦИИ (С СЕМАФОРОМ)
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
    # Получаем сессию
    session = sessions.get(task_id)
    if not session:
        await callback.message.edit_text("❌ Сессия истекла.")
        return
    if callback.from_user.id != session["user_id"] or callback.message.chat.id != session["chat_id"]:
        await callback.message.edit_text("❌ У вас нет доступа к этой задаче.")
        return

    task_dir = session["task_dir"]
    pptx_path = session["file_path"]
    if not task_dir.exists() or not pptx_path.exists():
        await callback.message.edit_text("❌ Файл не найден.")
        return

    # Блокировка задачи
    if not await task_lock_manager.acquire(task_id, "conversion"):
        await callback.answer("⏳ Задача уже обрабатывается.", show_alert=True)
        return

    # Ограничение одновременных конвертаций через семафор
    async with converter_semaphore:
        try:
            cfg = user_mgr.get_user_config(callback.from_user.id)
            chat_id = callback.message.chat.id
            user_id = callback.from_user.id

            if all_slides:
                expected_zip, final_pdf_path = await core_pipeline(pptx_path, callback.message, user_id, user_mgr)
                if expected_zip and expected_zip.exists():
                    if expected_zip.stat().st_size > 45 * 1024 * 1024:
                        await callback.message.edit_text(
                            "⚠️ **Архив слишком большой (>45 МБ).**\n"
                            "Telegram не позволяет отправлять файлы >50 МБ.\n"
                            "Попробуйте уменьшить качество или выбрать меньше слайдов."
                        )
                        return
                    await callback.message.edit_text("📤 Отправляю готовые файлы...")
                    await callback.bot.send_document(chat_id=chat_id, document=FSInputFile(expected_zip),
                                                    caption="📦 ZIP со всеми слайдами готов!")
                    if final_pdf_path and final_pdf_path.exists():
                        await callback.bot.send_document(chat_id=chat_id, document=FSInputFile(final_pdf_path),
                                                        caption="📄 PDF готов!")
                    await callback.message.delete()
                else:
                    await callback.message.edit_text("❌ Ошибка конвертации всех слайдов.")
                    return
            elif ranges:
                # Конвертация выбранных слайдов
                temp_png_dir = task_dir / "temp_pngs"
                temp_png_dir.mkdir(exist_ok=True)
                all_pngs = await convert_all_pngs(pptx_path, temp_png_dir, cfg["quality"])
                if not all_pngs:
                    await callback.message.edit_text("❌ Не удалось конвертировать слайды в PNG.")
                    return

                total_slides = len(all_pngs)
                archives = []
                for start, end in ranges:
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
                    zip_path = task_dir / f"{pptx_path.stem}_{range_name}.zip"

                    # Потоковое создание ZIP
                    create_zip_stream(selected, zip_path)

                    if zip_path.stat().st_size > 45 * 1024 * 1024:
                        zip_path.unlink()
                        await callback.message.edit_text(
                            f"⚠️ **Архив для диапазона {start}-{end} слишком большой (>45 МБ).**"
                        )
                        return
                    archives.append(zip_path)

                if archives:
                    await callback.message.edit_text(f"📤 Отправляю {len(archives)} архив(ов)...")
                    for zip_path in archives:
                        if zip_path.exists():
                            await callback.bot.send_document(chat_id=chat_id, document=FSInputFile(zip_path),
                                                            caption=f"📦 {zip_path.name}")
                            zip_path.unlink()  # Удаляем сразу после отправки
                    await callback.message.delete()
                    await callback.bot.send_message(
                        chat_id=chat_id,
                        text="⚙️ **Настройки для следующей презентации:**",
                        reply_markup=get_settings_keyboard(user_id)
                    )
                else:
                    await callback.message.edit_text("❌ Ошибка создания архивов.")
                    return

        except Exception as e:
            logging.error(f"Ошибка в run_conversion: {e}", exc_info=True)
            await callback.message.edit_text(f"❌ Ошибка конвертации: {e}")
        finally:
            # Очистка
            if task_dir.exists():
                shutil.rmtree(task_dir)
            sessions.pop(task_id, None)
            await task_lock_manager.release(task_id, "conversion")
