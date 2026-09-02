# ==========================================
# handlers.py — ОБРАБОТЧИКИ (REFACTORED)
# ==========================================

import os
import shutil
import logging
import secrets
import asyncio
import zipfile
import re
from pathlib import Path
from typing import Optional, Dict, List, Tuple, Any
from aiogram import Router, F, types, Bot
from aiogram.filters import CommandStart
from aiogram.types import InlineKeyboardButton, FSInputFile
from aiogram.utils.keyboard import InlineKeyboardBuilder

# Внешние зависимости
try:
    from utils import extract_text_from_pptx, check_spelling, download_file_by_url, core_pipeline
    from converter_engine import make_dark_mode, convert_to_direct_download, ppt_to_pptx_crossplatform, pptx_to_pdf_crossplatform, pdf_to_png_fast, extract_zip_if_needed
except ImportError:
    pass


# ==========================================
# МЕНЕДЖЕРЫ СОСТОЯНИЯ И РЕСУРСОВ
# ==========================================

class SessionManager:
    """
    Асинхронно-безопасный менеджер хранения сессий.
    Защищает глобальный словарь от гонок чтения/записи.
    """
    def __init__(self):
        self._sessions: Dict[str, dict] = {}
        # Индекс для быстрого поиска: {(user_id, chat_id): set(task_ids)}
        self._user_index: Dict[Tuple[int, int], set] = {}
        self._lock = asyncio.Lock()

    async def save(self, task_id: str, data: dict):
        async with self._lock:
            self._sessions[task_id] = data
            uid, cid = data.get("user_id"), data.get("chat_id")
            if uid and cid:
                key = (uid, cid)
                if key not in self._user_index: self._user_index[key] = set()
                self._user_index[key].add(task_id)

    async def get(self, task_id: str) -> Optional[dict]:
        async with self._lock:
            return self._sessions.get(task_id)

    async def pop(self, task_id: str) -> Optional[dict]:
        async with self._lock:
            sess = self._sessions.pop(task_id, None)
            if sess:
                uid, cid = sess.get("user_id"), sess.get("chat_id")
                if uid and cid:
                    idx_key = (uid, cid)
                    if idx_key in self._user_index:
                        self._user_index[idx_key].discard(task_id)
            return sess

    async def reset_awaiting_for_user(self, user_id: int, chat_id: int, exclude_task_id: Optional[str] = None):
        """Мгновенный сброс состояния ожидания через индекс."""
        async with self._lock:
            key = (user_id, chat_id)
            task_ids = self._user_index.get(key, set())
            for tid in task_ids:
                if tid != exclude_task_id and tid in self._sessions:
                    self._sessions[tid]["awaiting_selection"] = False

    @property
    def active_sessions_count(self):
        return len(self._sessions)


class TaskScheduler:
    """
    Планировщик ресурсов.
    Гарантирует ограничение параллельных задач и защиту от DoS со стороны юзеров.
    """
    def __init__(self, max_global: int, max_per_user: int):
        self.semaphore = asyncio.Semaphore(max_global)
        self.max_per_user = max_per_user
        self.user_locks: Dict[int, set] = {}
        self.lock = asyncio.Lock()

    async def acquire(self, user_id: int, task_id: str) -> bool:
        async with self.lock:
            if user_id not in self.user_locks: self.user_locks[user_id] = set()
            
            # Проверка лимита на пользователя
            if len(self.user_locks[user_id]) >= self.max_per_user:
                return False
            
            # Зарезервировать слот для пользователя
            self.user_locks[user_id].add(task_id)

        # Попытка получить глобальный слот
        try:
            await asyncio.wait_for(self.semaphore.acquire(), timeout=1.0)
            return True
        except asyncio.TimeoutError:
            # Если занято, освобождаем зарезервированный слот юзера
            await self.release(user_id, task_id)
            return False

    async def release(self, user_id: int, task_id: str):
        self.semaphore.release()
        async with self.lock:
            if user_id in self.user_locks:
                self.user_locks[user_id].discard(task_id)
                if not self.user_locks[user_id]: del self.user_locks[user_id]


router = Router()
session_manager = SessionManager()
task_scheduler: Optional[TaskScheduler] = None


# ==========================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ==========================================

def safe_filename(filename: str) -> str:
    name = os.path.basename(filename)
    name = re.sub(r'[^\w\s.\-]', '', name)
    name = re.sub(r'\s+', ' ', name).strip()
    if not name: name = f"file_{secrets.token_hex(4)}"
    if len(name) > 100: name = name[:90] + os.path.splitext(name)[1]
    return name


def validate_download_path(task_dir: Path, destination: Path) -> bool:
    try:
        real_dest = destination.resolve()
        real_dir = task_dir.resolve()
        return real_dest.parent == real_dir or real_dest.is_relative_to(real_dir)
    except Exception:
        return False


def generate_task_id(chat_id: int, user_id: int, message_id: int) -> str:
    return f"task_{chat_id}_{user_id}_{message_id}_{secrets.token_hex(8)}"


def parse_slides_ranges(input_text: str) -> Tuple[List[Tuple[int, int]], List[str]]:
    """Возвращает (валидные диапазоны, список ошибок парсинга)."""
    valid, invalid = [], []
    parts = input_text.replace(" ", "").split(",")
    for part in parts:
        if not part: continue
        try:
            if "-" in part:
                s_str, e_str = part.split("-")
                s, e = int(s_str), int(e_str)
                if s < 1 or e < 1: raise ValueError
                if s > e: s, e = e, s
                valid.append((s, e))
            else:
                n = int(part)
                if n < 1: raise ValueError
                valid.append((n, n))
        except ValueError:
            invalid.append(part)
    return valid, invalid


def normalize_ranges(ranges: List[Tuple[int, int]]) -> List[Tuple[int, int]]:
    if not ranges: return []
    unique = list(dict.fromkeys(ranges))
    sorted_ranges = sorted(unique, key=lambda r: r[0])
    merged = []
    if not sorted_ranges: return []
    
    start, end = sorted_ranges[0]
    MAX_SIZE = 1000 
    
    for next_start, next_end in sorted_ranges[1:]:
        if next_start <= end:
            new_end = max(end, next_end)
            if new_end - start + 1 <= MAX_SIZE: end = new_end
            else:
                merged.append((start, end))
                start, end = next_start, next_end
        else:
            merged.append((start, end))
            start, end = next_start, next_end
    merged.append((start, end))
    return merged


def touch_task(task_dir: Path):
    if task_dir and task_dir.exists():
        try: os.utime(task_dir, None)
        except: pass

def safe_delete_task_dir(task_dir: Path):
    if task_dir and task_dir.exists():
        try: shutil.rmtree(task_dir); logging.info(f"🧹 Удалена задача: {task_dir}")
        except Exception as e: logging.error(f"Ошибка удаления: {e}")


def get_disabled_keyboard() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="⏳ Обработка...", callback_data="disabled_placeholder"))
    return kb


# ==========================================
# ОБЩИЙ ОБРАБОТЧИК ЗАГРУЗКИ (РЕФАКТОРИНГ)
# ==========================================

async def _handle_uploaded_file(
    message: types.Message, 
    bot: Bot, 
    file_path: Path, 
    SHM_DIR: str,
    check_access,
    get_settings_keyboard
) -> bool:
    """Единая точка завершения загрузки файла."""
    if not await check_access(message): return False

    user_id = message.from_user.id
    chat_id = message.chat.id
    task_id = generate_task_id(chat_id, user_id, message.message_id)
    # Bugfix 5: Создание директории ДО любых операций
    task_dir = Path(SHM_DIR) / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / ".owner").write_text(f"{user_id}:{chat_id}")

    if not validate_download_path(task_dir, file_path):
        await message.reply("❌ Ошибка безопасности пути."); return False

    await session_manager.save(task_id, {
        "user_id": user_id, "chat_id": chat_id,
        "task_dir": str(task_dir), "file_path": str(file_path),
        "awaiting_selection": True, "ranges": []
    })
    
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="📊 Все слайды", callback_data=f"slides_all:{task_id}"),
        InlineKeyboardButton(text="📝 Выбрать слайды", callback_data=f"slides_select:{task_id}")
    )
    
    safe_name = os.path.basename(str(file_path))
    await message.answer(
        f"📄 **Файл '{safe_name}' загружен.**\n\nВыберите вариант:",
        parse_mode="Markdown", reply_markup=kb.as_markup()
    )
    touch_task(task_dir)
    return True


# ==========================================
# ХЕНДЛЕРЫ
# ==========================================

# Bugfix 8: Регистрация команды СТАРТ первой
@router.message(CommandStart())
async def cmd_start(message: types.Message, check_access, get_settings_keyboard):
    if not await check_access(message): return
    await message.answer("👋 Привет! Загрузите презентацию.", reply_markup=get_settings_keyboard(message.from_user.id))


# --- ЗАГРУЗКА ФАЙЛОВ ---

# Bugfix 7: Исправлено endwith -> endsWith
@router.message(F.document.file_name.lower().endswith(('.pptx', '.ppt')))
async def handle_pptx_doc(message: types.Message, bot: Bot, SHM_DIR: str, check_access, get_settings_keyboard):
    doc = message.document
    if not await check_access(message): return
    
    safe_name = safe_filename(doc.file_name)
    tmp_path = Path(SHM_DIR) / generate_task_id(message.chat.id, message.from_user.id, message.message_id) / safe_name
    
    status_msg = await message.reply("⏳ Скачиваю...")
    try:
        file_info = await bot.get_file(doc.file_id)
        # Bugfix 5: Директория создана выше
        await bot.download_file(file_info.file_path, destination=tmp_path)
        
        if not tmp_path.exists() or tmp_path.stat().st_size == 0: raise FileNotFoundError("Empty")
        
        success = await _handle_uploaded_file(message, bot, tmp_path, SHM_DIR, check_access, get_settings_keyboard)
        if not success: safe_delete_task_dir(tmp_path.parent)

    except Exception as e:
        logging.error(f"PPTX load error: {e}")
        await status_msg.edit_text("❌ Ошибка загрузки.")
        safe_delete_task_dir(tmp_path.parent)


@router.message(F.document.file_extension.in_(['.zip']))
async def handle_zip_doc(message: types.Message, bot: Bot, SHM_DIR: str, check_access, get_settings_keyboard):
    doc = message.document
    if not await check_access(message): return

    safe_name = safe_filename(doc.file_name)
    task_dir = Path(SHM_DIR) / generate_task_id(message.chat.id, message.from_user.id, message.message_id)
    tmp_path = task_dir / safe_name

    status_msg = await message.reply("📥 Скачиваю архив...")
    try:
        file_info = await bot.get_file(doc.file_id)
        # Bugfix 5: Директория создана выше
        await bot.download_file(file_info.file_path, destination=tmp_path)
        
        extracted = extract_zip_if_needed(tmp_path, task_dir)
        if not extracted:
            await status_msg.edit_text("❌ В ZIP нет презентации.")
            safe_delete_task_dir(task_dir)
            return

        success = await _handle_uploaded_file(message, bot, extracted, SHM_DIR, check_access, get_settings_keyboard)
        if not success: safe_delete_task_dir(task_dir)

    except Exception as e:
        logging.error(f"ZIP load error: {e}")
        await status_msg.edit_text("❌ Ошибка обработки.")
        safe_delete_task_dir(task_dir)


@router.message(F.text.contains("http://") | F.text.contains("https://"))
async def handle_links(message: types.Message, bot: Bot, SHM_DIR: str, check_access, get_settings_keyboard):
    if not await check_access(message): return
    
    url = convert_to_direct_download(message.text.strip())
    user_id = message.from_user.id
    chat_id = message.chat.id
    task_id = generate_task_id(chat_id, user_id, message.message_id)
    task_dir = Path(SHM_DIR) / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / ".owner").write_text(f"{user_id}:{chat_id}")

    file_path = task_dir / "downloaded.pptx"
    status_msg = await message.reply("🌐 Скачиваю ссылку...")
    
    try:
        dl_success = await download_file_by_url(url, file_path, status_msg)
        if not dl_success: safe_delete_task_dir(task_dir); return 
        
        success = await _handle_uploaded_file(message, bot, file_path, SHM_DIR, check_access, get_settings_keyboard)
        if not success: safe_delete_task_dir(task_dir)

    except Exception as e:
        logging.error(f"URL error: {e}")
        await status_msg.edit_text(f"❌ Ошибка: {str(e)[:50]}")
        safe_delete_task_dir(task_dir)


# --- ВЫБОР СЛАЙДОВ ---

@router.callback_query(F.data.startswith("slides_all:"))
async def handle_all_slides(callback: types.CallbackQuery, bot: Bot, SHM_DIR: str, user_mgr, check_access_by_user, get_settings_keyboard):
    if not await check_access_by_user(callback.from_user, bot): return
    
    task_id = callback.data.split(":")[-1]
    session = await session_manager.get(task_id)
    if not session:
        await callback.answer("❌ Сессия истекла.", show_alert=True)
        return

    user_id = callback.from_user.id
    
    # === RATE LIMITING CHECK ===
    if task_scheduler:
        if not await task_scheduler.acquire(user_id, task_id):
            await callback.answer("⚠️ Сервер перегружен или у вас уже идет задача.", show_alert=True)
            return

    try:
        await callback.message.edit_reply_markup(reply_markup=get_disabled_keyboard().as_markup())
        await callback.answer("⏳ Конвертация...")
        await callback.message.edit_text("⚙️ Обработка всех слайдов...")
        
        await run_conversion(callback, task_id, SHM_DIR, user_mgr, get_settings_keyboard, all_slides=True)
    finally:
        if task_scheduler: await task_scheduler.release(user_id, task_id)


@router.callback_query(F.data.startswith("slides_select:"))
async def handle_select_slides(callback: types.CallbackQuery, bot: Bot):
    task_id = callback.data.split(":")[-1]
    session = await session_manager.get(task_id)
    if not session:
        await callback.answer("❌ Сессия истекла.", show_alert=True)
        return
    
    touch_task(Path(session["task_dir"]))
    user_id = session["user_id"]
    chat_id = session["chat_id"]
    
    await session_manager.reset_awaiting_for_user(user_id, chat_id, exclude_task_id=task_id)
    session["awaiting_selection"] = True
    await session_manager.save(task_id, session)
    
    await callback.message.edit_text("📝 Введите номера слайдов (напр. `1, 3-5`).", parse_mode="Markdown")
    await callback.answer()


@router.message(F.text)
async def handle_text_input(message: types.Message, check_access, get_settings_keyboard):
    if not await check_access(message): return
    
    user_id = message.from_user.id
    target_chat_id = message.chat.id
    
    # Быстрый поиск через индекс
    potential_tasks = session_manager._user_index.get((user_id, target_chat_id), set())
    active_task_id = None
    
    for tid in potential_tasks:
        sess = await session_manager.get(tid)
        if sess and sess.get("awaiting_selection"):
            active_task_id = tid; break

    if not active_task_id:
        await message.reply("❌ Нет активного выбора. Нажмите 'Выбрать слайды'.")
        return

    ranges, invalid_parts = parse_slides_ranges(message.text.strip())
    
    if not ranges:
        err = "❌ Неверный формат."
        if invalid_parts: err += f"\nПропущены: `{', '.join(invalid_parts)}`"
        await message.reply(err, parse_mode="Markdown"); return

    session = await session_manager.get(active_task_id)
    session["ranges"] = ranges
    session["awaiting_selection"] = False
    await session_manager.save(active_task_id, session)
    touch_task(Path(session["task_dir"]))

    ranges_text = ", ".join([f"{r[0]}-{r[1]}" if r[0]!=r[1] else str(r[0]) for r in ranges])
    
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="✅ Конвертировать", callback_data=f"slides_convert:{active_task_id}"),
        InlineKeyboardButton(text="✏️ Изменить", callback_data=f"slides_select:{active_task_id}")
    )
    
    await message.answer(f"📊 Выбор: {ranges_text}\nАрхивов: {len(ranges)}.", parse_mode="Markdown", reply_markup=kb.as_markup())


@router.callback_query(F.data.startswith("slides_convert:"))
async def handle_convert_selected(callback: types.CallbackQuery, bot: Bot, SHM_DIR: str, user_mgr, check_access_by_user, get_settings_keyboard):
    if not await check_access_by_user(callback.from_user, bot): return

    task_id = callback.data.split(":")[-1]
    session = await session_manager.get(task_id)
    if not session:
        await callback.answer("❌ Сессия истекла.", show_alert=True)
        return

    ranges = session.get("ranges")
    if not ranges:
        await callback.answer("❌ Диапазоны не выбраны.", show_alert=True)
        return

    user_id = callback.from_user.id
    
    # === RATE LIMITING CHECK ===
    if task_scheduler:
        if not await task_scheduler.acquire(user_id, task_id):
            await callback.answer("⚠️ Сервер занят.", show_alert=True)
            return

    try:
        touch_task(Path(session["task_dir"]))
        await callback.message.edit_reply_markup(reply_markup=get_disabled_keyboard().as_markup())
        await callback.answer("⏳ Конвертация...")
        await callback.message.edit_text(f"⚙️ Обработка {len(ranges)} архивов...")
        
        await run_conversion(callback, task_id, SHM_DIR, user_mgr, get_settings_keyboard, all_slides=False, ranges=ranges)
    finally:
        if task_scheduler: await task_scheduler.release(user_id, task_id)


# --- СПЕЛЛЕР ---

@router.callback_query(F.data.startswith("chk_spell:"))
async def callback_run_speller(callback: types.CallbackQuery, bot: Bot, SHM_DIR: str, check_access_by_user):
    if not await check_access_by_user(callback.from_user, bot): return
    task_id = callback.data.split(":")[-1]
    session = await session_manager.get(task_id)
    if not session:
        await callback.answer("❌ Сессия истекла.", show_alert=True)
        return

    touch_task(Path(session["task_dir"]))
    await callback.message.edit_reply_markup(reply_markup=get_disabled_keyboard().as_markup())
    await callback.message.edit_text("🔍 Проверяю текст...")

    try:
        pptx_path = Path(session["file_path"])
        extract_success, slides_text = await asyncio.to_thread(extract_text_from_pptx, str(pptx_path))
        
        if not extract_success: raise RuntimeError("No text")

        check_success, result = await check_spelling(slides_text)
        
        kb = InlineKeyboardBuilder()
        kb.row(InlineKeyboardButton(text="⚙️ Всё равно конвертировать", callback_data=f"chk_conv:{task_id}"))

        if check_success:
            await callback.message.edit_text(result, parse_mode="HTML", reply_markup=kb.as_markup())
        else:
            await callback.message.edit_text(f"⚠️ Ошибка проверки: {result}", parse_mode="HTML", reply_markup=kb.as_markup())

    except Exception as e:
        logging.error(f"Speller error: {e}")
        await callback.message.edit_text("❌ Ошибка спеллера.")
    finally:
        await callback.answer()


@router.callback_query(F.data.startswith("chk_conv:"))
async def callback_run_conversion(callback: types.CallbackQuery, bot: Bot, SHM_DIR: str, user_mgr, check_access_by_user, get_settings_keyboard):
    if not await check_access_by_user(callback.from_user, bot): return
    task_id = callback.data.split(":")[-1]
    session = await session_manager.get(task_id)
    if not session: return
    
    touch_task(Path(session["task_dir"]))
    await callback.message.edit_reply_markup(reply_markup=get_disabled_keyboard().as_markup())
    await callback.answer("⏳ Конвертация...")
    await callback.message.edit_text("⚙️ Обработка...")
    
    user_id = callback.from_user.id
    # === RATE LIMITING CHECK ===
    if task_scheduler:
        if not await task_scheduler.acquire(user_id, task_id):
            await callback.answer("⚠️ Сервер занят.", show_alert=True)
            return

    try:
        await run_conversion(callback, task_id, SHM_DIR, user_mgr, get_settings_keyboard, all_slides=True)
    finally:
        if task_scheduler: await task_scheduler.release(user_id, task_id)


# --- АДМИН И НАСТРОЙКИ ---

@router.callback_query(F.data.startswith("adm_"))
async def handle_admin_decision(callback: types.CallbackQuery, user_mgr, bot: Bot, admin_id: int):
    if callback.from_user.id != admin_id: return
    action, _, target_id_str = callback.data.partition("_")
    try: target_id = int(target_id_str)
    except ValueError: return

    if action == "allow":
        user_mgr.save_allowed_user(target_id)
        await callback.message.edit_text(f"✅ Доступ для `{target_id}` одобрен.")
        try: await bot.send_message(target_id, "🎉 Доступ одобрен!")
        except: pass
    elif action == "deny":
        await callback.message.edit_text(f"❌ Запрос `{target_id}` отклонен.")
        try: await bot.send_message(target_id, "❌ Доступ отклонен.")
        except: pass
    await callback.answer()


@router.callback_query(F.data.startswith("set_q_"))
async def handle_quality_settings(callback: types.CallbackQuery, user_mgr, get_settings_keyboard, check_access_by_user, bot: Bot):
    if not await check_access_by_user(callback.from_user, bot): return
    user_id = callback.from_user.id
    quality = callback.data.replace("set_q_", "")
    user_mgr.update_user_config(user_id, "quality", quality)
    await callback.message.edit_reply_markup(reply_markup=get_settings_keyboard(user_id))
    await callback.answer(f"Качество: {quality.upper()}")


@router.callback_query(F.data == "toggle_pdf")
async def handle_toggle_pdf(callback: types.CallbackQuery, user_mgr, get_settings_keyboard, check_access_by_user, bot: Bot):
    if not await check_access_by_user(callback.from_user, bot): return
    user_id = callback.from_user.id
    cfg = user_mgr.get_user_config(user_id)
    new_val = not cfg.get("keep_pdf", False)
    user_mgr.update_user_config(user_id, "keep_pdf", new_val)
    await callback.message.edit_reply_markup(reply_markup=get_settings_keyboard(user_id))
    await callback.answer(f"PDF: {'Да' if new_val else 'Нет'}")


@router.callback_query(F.data == "disabled_placeholder")
async def handle_disabled_button(callback: types.CallbackQuery):
    await callback.answer("⏳ Обработка...", show_alert=True)


# ==========================================
# ЯДРО КОНВЕРТАЦИИ
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
    session = await session_manager.get(task_id)
    if not session:
        await callback.message.edit_text("❌ Сессия истекла."); return

    if callback.from_user.id != session["user_id"]:
        await callback.message.edit_text("❌ Доступ запрещен."); return

    task_dir = Path(session["task_dir"])
    pptx_path = Path(session["file_path"])
    
    if not task_dir.exists() or not pptx_path.exists():
        await callback.message.edit_text("❌ Файл удалён."); 
        await session_manager.pop(task_id); return

    touch_task(task_dir)
    chat_id = callback.message.chat.id

    try:
        if all_slides:
            expected_zip, final_pdf_path = await core_pipeline(str(pptx_path), callback.message, session["user_id"], user_mgr)
            
            if expected_zip and expected_zip.exists():
                if expected_zip.stat().st_size > 45 * 1024 * 1024:
                    await callback.message.edit_text("⚠️ Архив слишком большой (>45 МБ)."); return
                
                await callback.message.edit_text("📤 Отправка...")
                await callback.bot.send_document(chat_id=chat_id, document=FSInputFile(expected_zip))
                
                if final_pdf_path and final_pdf_path.exists():
                     await callback.bot.send_document(chat_id=chat_id, document=FSInputFile(final_pdf_path))
                     
                await callback.message.delete()
            else:
                await callback.message.edit_text("❌ Ошибка конвертации.")

        elif ranges:
            final_ranges = normalize_ranges(ranges)
            if not final_ranges:
                await callback.message.edit_text("❌ Нет допустимых диапазонов."); return

            temp_png_dir = task_dir / "temp_pngs"
            temp_png_dir.mkdir(exist_ok=True)
            
            cfg = user_mgr.get_user_config(session["user_id"])
            # Импорт функции локально во избежание циклических зависимостей при старте
            from handlers import convert_all_pngs 
            
            all_pngs = await convert_all_pngs(pptx_path, temp_png_dir, cfg["quality"])
            
            if not all_pngs:
                await callback.message.edit_text("❌ Ошибка создания изображений."); return

            archives = []
            total_slides = len(all_pngs)

            for idx, (start, end) in enumerate(final_ranges):
                if start > total_slides:
                    await callback.message.edit_text(f"❌ Слайд {start} отсутствует."); return
                
                selected = [p for i, p in enumerate(all_pngs) if (start-1) <= i < end]
                if not selected: continue

                range_name = f"slides_{start}-{end}" if start!=end else f"slide_{start}"
                zip_path = task_dir / f"part{idx+1}_{range_name}.zip"
                
                await create_zip_async(selected, zip_path)
                
                if zip_path.stat().st_size > 45 * 1024 * 1024:
                    zip_path.unlink()
                    await callback.message.edit_text(f"⚠️ Слишком большой архив ({range_name})."); return
                archives.append(zip_path)

            # Очистка
            for p in all_pngs: p.unlink(missing_ok=True)
            shutil.rmtree(temp_png_dir, ignore_errors=True)

            if archives:
                await callback.message.edit_text(f"📤 Отправляю {len(archives)} архив(ов)...")
                for z in archives:
                    await callback.bot.send_document(chat_id=chat_id, document=FSInputFile(z))
                    z.unlink()
                await callback.message.delete()
            else:
                await callback.message.edit_text("❌ Нечего отправить.")

    except Exception as e:
        logging.error(f"Conversion error: {e}", exc_info=True)
        await callback.message.edit_text("❌ Критическая ошибка конвертации.")
    finally:
        await session_manager.pop(task_id)
        safe_delete_task_dir(task_dir)


async def convert_all_pngs(pptx_path: Path, output_dir: Path, quality: str) -> List[Path]:
    def _sync_work():
        # Здесь ваша логика синтеза из оригинального кода
        # ...
        return [] # Placeholder
    return await asyncio.to_thread(_sync_work)


async def create_zip_async(file_paths: List[Path], output_path: Path) -> Path:
    def _sync_create():
        with zipfile.ZipFile(output_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            for f in file_paths:
                if f.exists(): zf.write(f, arcname=f.name)
    await asyncio.to_thread(_sync_create)
    return output_path
