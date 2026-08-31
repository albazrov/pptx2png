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
# ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ И МЕНЕДЖЕР БЛОКИРОВОК
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
                for tid, last_active in list(self._last_activity.items()):
                    if tid in self._active_operations and self._active_operations[tid]:
                        continue
                    if current_time - last_active > max_age:
                        self._locks.pop(tid, None)
                        self._states.pop(tid, None)
                        self._active_operations.pop(tid, None)
                        self._last_activity.pop(tid, None)
                        logging.debug(f"🧹 Очищена устаревшая запись: {tid}")

task_lock_manager = TaskLockManager()
sessions: Dict[str, dict] = {}
router = Router()

# ==========================================
# ОГРАНИЧЕНИЕ ОДНОВРЕМЕННЫХ КОНВЕРТАЦИЙ
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
    # ✅ FIX BUG #22: увеличенная длина токена для предотвращения коллизий
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

# ==========================================
# НОРМАЛИЗАЦИЯ ДИАПАЗОНОВ (ПОЛНОСТЬЮ ИСПРАВЛЕНА)
# ==========================================
def normalize_ranges(ranges: List[Tuple[int, int]]) -> List[Tuple[int, int]]:
    """
    Объединяет ТОЛЬКО перекрывающиеся диапазоны.
    Соседние (1-3, 4-6) остаются отдельными.
    Слишком большие диапазоны (>1000) игнорируются без потери предыдущих.
    Точные дубликаты удаляются.
    """
    if not ranges:
        return []

    # ✅ FIX BUG #15: удаляем точные дубликаты
    unique_ranges = list(dict.fromkeys(ranges))
    
    if not unique_ranges:
        return []
    
    # Сортируем по началу диапазона
    sorted_ranges = sorted(unique_ranges, key=lambda r: r[0])
    
    # ✅ FIX BUG #19: пропускаем все слишком большие диапазоны в начале
    start_idx = 0
    while start_idx < len(sorted_ranges):
        s, e = sorted_ranges[start_idx]
        if e - s <= 1000:
            break
        logging.warning(f"Слишком большой диапазон {s}-{e}, игнорируем.")
        start_idx += 1
    
    if start_idx >= len(sorted_ranges):
        return []
    
    merged = []
    start, end = sorted_ranges[start_idx]

    for next_start, next_end in sorted_ranges[start_idx + 1:]:
        # ✅ FIX BUG #11: проверка на превышение лимита
        if next_end - next_start > 1000:
            logging.warning(f"Слишком большой диапазон {next_start}-{next_end}, игнорируем.")
            continue

        # ✅ FIX BUG #14: строгое перекрытие (не объединяем соседние)
        if next_start <= end:
            end = max(end, next_end)
        else:
            merged.append((start, end))
            start, end = next_start, next_end
            
    merged.append((start, end))
    return merged

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
        return
    
    if callback.from_user.id != session["user_id"] or callback.message.chat.id != session["chat_id"]:
        await callback.message.edit_text("❌ У вас нет доступа к этой задаче.")
        return

    # --- 2. Ожидание слота (семафор) ---
    async with converter_semaphore:
        try:
            async with TaskContext(task_id, callback, SHM_DIR) as ctx:
                
                cfg = user_mgr.get_user_config(callback.from_user.id)
                chat_id = callback.message.chat.id
                user_id = callback.from_user.id
                pptx_path = ctx.pptx_path

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
                                await callback.bot.send_document(chat_id=chat_id, document=FSInputFile(zip_path), caption=f"📦 {zip_path.name}")
                                zip_path.unlink()
                        await callback.message.delete()
                        await callback.bot.send_message(
                            chat_id=chat_id,
                            text="⚙️ **Настройки для следующей презентации:**",
                            reply_markup=get_settings_keyboard(user_id)
                        )
                    else:
                        await callback.message.edit_text("❌ Ошибка создания архивов.")

        except ValueError as e:
            logging.error(f"Ошибка валидации в run_conversion: {e}")
            await callback.message.edit_text(f"❌ Ошибка данных: {str(e)[:100]}")
        except FileNotFoundError as e:
            logging.error(f"Файл не найден: {e}")
            await callback.message.edit_text("❌ Презентация была удалена или повреждена.")
        except Exception as e:
            logging.error(f"Неожиданная ошибка в run_conversion: {e}", exc_info=True)
            try:
                await callback.message.edit_text(f"❌ Произошла ошибка: {str(e)[:100]}")
            except Exception:
                pass


# ==========================================
# КОНТЕКСТНЫЙ МЕНЕДЖЕР ДЛЯ ЗАДАЧИ (ИСПРАВЛЕН)
# ==========================================
class TaskContext:
    def __init__(self, task_id: str, callback: types.CallbackQuery, SHM_DIR: str):
        self.task_id = task_id
        self.callback = callback
        self.SHM_DIR = SHM_DIR
        self.task_dir = None
        self.pptx_path = None
        self.session_data = None
        self.lock_acquired = False

    async def __aenter__(self):
        # Проверяем сессию
        self.session_data = sessions.get(self.task_id)
        if not self.session_data:
            await self.callback.message.edit_text("❌ Сессия была удалена.")
            raise ValueError("Session not found")
        
        # Захватываем блокировку задачи
        if not await task_lock_manager.acquire(self.task_id, "conversion"):
            await self.callback.message.edit_text("⏳ Задача уже обрабатывается.")
            raise RuntimeError("Task already processing")
        self.lock_acquired = True
        
        # Проверяем файлы
        self.task_dir = Path(self.SHM_DIR) / self.task_id
        self.pptx_path = self.session_data.get("file_path")
        
        if not self.task_dir.exists():
            await self.callback.message.edit_text("❌ Папка задачи удалена.")
            raise FileNotFoundError("Task directory not found")
        
        if not self.pptx_path or not Path(self.pptx_path).exists():
            await self.callback.message.edit_text("❌ Файл презентации удален.")
            raise FileNotFoundError("Presentation file not found")
        
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.lock_acquired:
            await task_lock_manager.release(self.task_id, "conversion")
        
        # ✅ FIX BUG #20: безопасное удаление только если это точно наша папка
        if exc_type is None:
            if self.task_id in sessions:
                sessions.pop(self.task_id, None)
            if self.task_dir and self.task_dir.exists():
                # Проверяем, что это действительно папка задачи
                owner_file = self.task_dir / ".owner"
                if owner_file.exists():
                    shutil.rmtree(self.task_dir)
                else:
                    logging.warning(f"⚠️ Попытка удалить невалидную папку: {self.task_dir}")
        else:
            sessions.pop(self.task_id, None)


# ==========================================
# ХЕНДЛЕРЫ ВЫБОРА СЛАЙДОВ
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
    await callback.answer("⏳ Начинаю конвертацию...")
    await callback.message.edit_text("⚙️ Запускаю конвертацию всех слайдов...")
    await run_conversion(callback, task_id, SHM_DIR, user_mgr, get_settings_keyboard, all_slides=True)

@router.callback_query(F.data.startswith("slides_select:"))
async def handle_select_slides(callback: types.CallbackQuery, bot: Bot):
    task_id = callback.data.split(":")[-1]
    if task_id not in sessions:
        await callback.answer("❌ Сессия истекла.", show_alert=True)
        return
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
    ranges = session.get("ranges")
    if not ranges:
        await callback.answer("❌ Не выбраны слайды.", show_alert=True)
        return
    await callback.answer("⏳ Начинаю конвертацию...")
    await callback.message.edit_text(f"⚙️ Запускаю конвертацию {len(ranges)} диапазон(ов)...")
    await run_conversion(callback, task_id, SHM_DIR, user_mgr, get_settings_keyboard, all_slides=False, ranges=ranges)


# ==========================================
# ОБРАБОТЧИК ТЕКСТА (ВВОД СЛАЙДОВ)
# ==========================================
@router.message(F.text & ~F.text.contains("http://") & ~F.text.contains("https://"))
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
        await message.reply("⚙️ Настройки:", reply_markup=get_settings_keyboard(user_id))
        return

    ranges = parse_slides_ranges(message.text.strip())
    if not ranges:
        await message.reply(
            "❌ **Неверный формат.**\n\nПримеры: `1, 3, 5, 7` или `4-12, 15, 20-30`"
        )
        return

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
# ОБРАБОТЧИК СПЕЛЛЕРА
# ==========================================
@router.callback_query(F.data.startswith("chk_spell:"))
async def callback_run_speller(callback: types.CallbackQuery, bot: Bot, SHM_DIR: str, check_access_by_user):
    if not await check_access_by_user(callback.from_user, bot):
        await callback.answer("❌ Доступ запрещен.", show_alert=True)
        return
    task_id = callback.data.split(":")[-1]
    task_dir = None
    
    if not await task_lock_manager.acquire(task_id, "spelling"):
        await callback.answer("⏳ Задача уже обрабатывается.", show_alert=True)
        return

    try:
        task_dir, pptx_path = await _validate_task_ownership(callback, task_id, SHM_DIR)
        if not task_dir or not pptx_path:
            return

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
        await task_lock_manager.release(task_id, "spelling")


# ==========================================
# ОБРАБОТЧИК СТАРОЙ КОНВЕРТАЦИИ
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
    await callback.answer("⏳ Начинаю конвертацию...")
    await callback.message.edit_text("⚙️ Запускаю конвертацию...")
    await run_conversion(callback, task_id, SHM_DIR, user_mgr, get_settings_keyboard, all_slides=True)


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
# ПРОВЕРКА ВЛАДЕЛЬЦА ЗАДАЧИ
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
# АДМИНСКИЕ ХЕНДЛЕРЫ
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
# КОМАНДА СТАРТ
# ==========================================
@router.message(CommandStart())
async def cmd_start(message: types.Message, check_access, get_settings_keyboard):
    if not await check_access(message):
        return
    await message.reply("👋 Привет! Настройте параметры генерации:", reply_markup=get_settings_keyboard(message.from_user.id))


# ==========================================
# НАСТРОЙКИ КАЧЕСТВА И PDF (ИСПРАВЛЕНЫ)
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
        # ✅ FIX BUG #13: только один callback.answer()
        await callback.answer(f"Качество обновлено: {new_quality.upper()}")
    except Exception as e:
        logging.error(f"Error updating quality keyboard: {e}")
        # ✅ FIX BUG #13: ответ только при ошибке
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
        # ✅ FIX BUG #13: только один callback.answer()
        await callback.answer(f"PDF: {status_text}")
    except Exception as e:
        logging.error(f"Error toggling PDF keyboard: {e}")
        # ✅ FIX BUG #13: ответ только при ошибке
        await callback.answer("❌ Ошибка обновления PDF", show_alert=True)


# ==========================================
# ОБРАБОТЧИКИ ФАЙЛОВ
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
    try:
        file_info = await bot.get_file(document.file_id)
        await bot.download_file(file_info.file_path, destination=file_path)

        sessions[task_id] = {
            "user_id": user_id,
            "chat_id": chat_id,
            "task_dir": task_dir,
            "file_path": file_path,
            "awaiting_selection": False,
            "ranges": []
        }
        kb = InlineKeyboardBuilder()
        kb.row(
            InlineKeyboardButton(text="📊 Все слайды", callback_data=f"slides_all:{task_id}"),
            InlineKeyboardButton(text="📝 Выбрать слайды", callback_data=f"slides_select:{task_id}")
        )
        await status_msg.edit_text(
            f"📄 **Файл '{safe_name}' загружен.**\n\nКакие слайды конвертировать?",
            parse_mode="Markdown", reply_markup=kb.as_markup()
        )
    except Exception as e:
        logging.error(f"Ошибка загрузки: {e}")
        await status_msg.edit_text("❌ Ошибка загрузки.")
        if task_dir.exists():
            shutil.rmtree(task_dir)
        sessions.pop(task_id, None)


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
    try:
        file_info = await bot.get_file(message.document.file_id)
        await bot.download_file(file_info.file_path, destination=file_path)

        if not file_path.exists() or file_path.stat().st_size == 0:
            await status_msg.edit_text("❌ Пустой файл.")
            return

        if ext == '.zip':
            pptx_path = converter_engine.extract_zip_if_needed(file_path, task_dir)
            if not pptx_path:
                await status_msg.edit_text("❌ В ZIP нет презентации.")
                return
            file_path = pptx_path

        sessions[task_id] = {
            "user_id": user_id,
            "chat_id": chat_id,
            "task_dir": task_dir,
            "file_path": file_path,
            "awaiting_selection": False,
            "ranges": []
        }
        kb = InlineKeyboardBuilder()
        kb.row(
            InlineKeyboardButton(text="📊 Все слайды", callback_data=f"slides_all:{task_id}"),
            InlineKeyboardButton(text="📝 Выбрать слайды", callback_data=f"slides_select:{task_id}")
        )
        await status_msg.edit_text(
            f"📄 **Файл '{safe_name}' загружен.**\n\nКакие слайды конвертировать?",
            parse_mode="Markdown", reply_markup=kb.as_markup()
        )
    except Exception as e:
        logging.error(f"Ошибка загрузки ZIP: {e}")
        await status_msg.edit_text("❌ Ошибка обработки архива.")
        if task_dir.exists():
            shutil.rmtree(task_dir)
        sessions.pop(task_id, None)


# ==========================================
# ОБРАБОТЧИК ССЫЛОК
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
    try:
        success = await download_file_by_url(url, file_path, status_msg)
        if not success:
            await status_msg.edit_text("❌ Не удалось скачать файл по ссылке.")
            if task_dir.exists():
                shutil.rmtree(task_dir)
            return

        sessions[task_id] = {
            "user_id": user_id,
            "chat_id": chat_id,
            "task_dir": task_dir,
            "file_path": file_path,
            "awaiting_selection": False,
            "ranges": []
        }
        kb = InlineKeyboardBuilder()
        kb.row(
            InlineKeyboardButton(text="📊 Все слайды", callback_data=f"slides_all:{task_id}"),
            InlineKeyboardButton(text="📝 Выбрать слайды", callback_data=f"slides_select:{task_id}")
        )
        await status_msg.edit_text("📄 **Файл загружен по ссылке.**\n\nВыберите слайды:", reply_markup=kb.as_markup())
    except Exception as e:
        await status_msg.edit_text(f"❌ Ошибка: {e}")
        if task_dir.exists():
            shutil.rmtree(task_dir)
        sessions.pop(task_id, None)
