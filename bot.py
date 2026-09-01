import sys
import os
import logging
import asyncio
import shutil
import configparser
import argparse
import signal
import time
from pathlib import Path
from logging.handlers import RotatingFileHandler

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart
from aiogram.types import FSInputFile, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
import aiohttp

import converter_engine
from user_manager import UserManager
from handlers import router, sessions, task_lock_manager


# ==========================================
# 1. НАСТРОЙКА ОКРУЖЕНИЯ
# ==========================================

def setup_environment():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    env_name = os.path.basename(script_dir)

    parser = argparse.ArgumentParser(description="PPTX2PNG Telegram Bot")
    parser.add_argument("--log-dir", type=str, help="Путь к папке логов")
    parser.add_argument("--shm-dir", type=str, help="Путь к временной папке в RAM-диске")
    args, unknown = parser.parse_known_args()

    config_path = Path(script_dir) / "config.ini"
    settings_path = Path(script_dir) / "settings.ini"

    config = configparser.ConfigParser()
    settings_config = configparser.ConfigParser()

    if not config_path.exists():
        sys.exit(f"❌ Ошибка: Файл секретов config.ini не найден по пути: {config_path}")
    config.read(config_path, encoding='utf-8')

    try:
        bot_token = config.get("Telegram", "BOT_TOKEN").strip()
        admin_id = int(config.get("Telegram", "ADMIN_ID").strip())
    except Exception as e:
        sys.exit(f"❌ Ошибка в config.ini: {e}")

    if settings_path.exists():
        settings_config.read(settings_path, encoding='utf-8')

    if args.shm_dir:
        shm_dir = Path(args.shm_dir)
    else:
        try:
            base_shm = settings_config.get("Paths", "shm_dir").strip()
            if not base_shm:
                raise configparser.NoOptionError("shm_dir", "Paths")
            shm_dir = Path(base_shm) / env_name
        except (configparser.NoSectionError, configparser.NoOptionError):
            shm_dir = Path("/dev/shm/pptx2png_tasks") / env_name

    shm_dir.mkdir(parents=True, exist_ok=True)

    if args.log_dir:
        log_dir = args.log_dir
    else:
        try:
            base_log = settings_config.get("Paths", "log_dir").strip()
            if not base_log:
                raise configparser.NoOptionError("log_dir", "Paths")
            log_dir = base_log
        except (configparser.NoSectionError, configparser.NoOptionError):
            log_dir = os.path.join(str(shm_dir), "logs")

    os.makedirs(log_dir, exist_ok=True)

    return script_dir, env_name, bot_token, admin_id, shm_dir, log_dir


# ==========================================
# 2. НАСТРОЙКА ЛОГИРОВАНИЯ С РОТАЦИЕЙ
# ==========================================

def setup_logging(log_dir: str):
    log_formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)

    info_handler = RotatingFileHandler(
        os.path.join(log_dir, "bot.log"),
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding='utf-8'
    )
    info_handler.setLevel(logging.INFO)
    info_handler.setFormatter(log_formatter)
    root_logger.addHandler(info_handler)

    debug_handler = RotatingFileHandler(
        os.path.join(log_dir, "debug.log"),
        maxBytes=10 * 1024 * 1024,
        backupCount=3,
        encoding='utf-8'
    )
    debug_handler.setLevel(logging.DEBUG)
    debug_handler.setFormatter(log_formatter)
    root_logger.addHandler(debug_handler)

    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setLevel(logging.INFO)
    stdout_handler.setFormatter(log_formatter)
    root_logger.addHandler(stdout_handler)


def escape_markdown(text: str) -> str:
    special_chars = r'_*[]()~`>#+-=|{}.!'
    for char in special_chars:
        text = text.replace(char, f'\\{char}')
    return text


# ==========================================
# 3. БЕЗОПАСНАЯ ОЧИСТКА СТАРЫХ ЗАДАЧ
# ==========================================

def is_process_alive(pid: int) -> bool:
    """Проверяет, существует ли процесс с указанным PID."""
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False
    except Exception:
        return False

def get_owner_info(task_dir: Path) -> tuple:
    """Возвращает (pid, timestamp) из папки задачи."""
    pid_file = task_dir / ".pid"
    if not pid_file.exists():
        return None, None
    
    try:
        content = pid_file.read_text().strip()
        parts = content.split(":")
        if len(parts) >= 2:
            pid = int(parts[0])
            timestamp = float(parts[1]) if parts[1] else None
            return pid, timestamp
        return int(content), None
    except Exception:
        return None, None

def cleanup_old_tasks(shm_dir: Path, max_age_seconds: int = 1800):
    """
    Безопасная очистка старых папок задач.
    Удаляет папки, если:
    1. Процесс-владелец не жив (PID не существует).
    2. ИЛИ папка старше max_age_seconds (защита от зависших процессов).
    """
    if not shm_dir.exists():
        return
    
    current_time = time.time()
    deleted_count = 0
    my_pid = os.getpid()
    
    for item in shm_dir.iterdir():
        if not item.is_dir() or not item.name.startswith("task_"):
            continue
        
        pid, timestamp = get_owner_info(item)
        
        # Папка нашего процесса — никогда не удаляем
        if pid == my_pid:
            logging.debug(f"📁 Папка {item.name} принадлежит текущему процессу, пропускаем")
            continue
        
        should_delete = False
        reason = ""
        
        if pid is None:
            should_delete = True
            reason = "нет информации о владельце"
        elif not is_process_alive(pid):
            should_delete = True
            reason = f"процесс {pid} не существует"
        elif timestamp and (current_time - timestamp) > max_age_seconds:
            should_delete = True
            reason = f"старше {max_age_seconds} секунд"
        
        if should_delete:
            try:
                shutil.rmtree(item)
                deleted_count += 1
                logging.info(f"🧹 Удалена папка {item.name} ({reason})")
            except Exception as e:
                logging.error(f"❌ Ошибка удаления папки {item}: {e}")
    
    if deleted_count > 0:
        logging.info(f"🧹 Очищено {deleted_count} старых папок задач")


# ==========================================
# 4. СОЗДАНИЕ БОТА И ДИСПЕТЧЕРА
# ==========================================

def create_bot_and_dispatcher(bot_token: str, admin_id: int, shm_dir: Path, script_dir: str):
    bot = Bot(token=bot_token)
    dp = Dispatcher()

    user_mgr = UserManager(admin_id=admin_id, base_dir=Path(script_dir))
    http_session = aiohttp.ClientSession()

    def get_settings_keyboard(user_id):
        cfg = user_mgr.get_user_config(user_id)
        q_std = "✅ Standard" if cfg["quality"] == "standard" else "Standard"
        q_2k = "✅ 2K" if cfg["quality"] == "2k" else "2K"
        q_4k = "✅ 4K" if cfg["quality"] == "4k" else "4K"
        pdf_status = "✅ Да (ZIP + PDF)" if cfg["keep_pdf"] else "❌ Нет (Только ZIP)"

        builder = InlineKeyboardBuilder()
        builder.row(
            InlineKeyboardButton(text=q_std, callback_data="set_q_standard"),
            InlineKeyboardButton(text=q_2k, callback_data="set_q_2k"),
            InlineKeyboardButton(text=q_4k, callback_data="set_q_4k")
        )
        builder.row(InlineKeyboardButton(text=f"Возвращать PDF: {pdf_status}", callback_data="toggle_pdf"))
        return builder.as_markup()

    async def check_access_by_user(user: types.User, bot: Bot) -> bool:
        user_id = user.id
        if user_id in user_mgr.load_allowed_users():
            return True

        admin_kb = InlineKeyboardBuilder()
        admin_kb.row(
            InlineKeyboardButton(text="✅ Разрешить", callback_data=f"adm_allow_{user_id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"adm_deny_{user_id}")
        )

        try:
            await bot.send_message(
                chat_id=admin_id,
                text=(
                    f"🔔 <b>Запрос доступа!</b>\n\n"
                    f"• <b>Имя:</b> <code>{user.full_name or 'без имени'}</code>\n"
                    f"• <b>Юзернейм:</b> <code>@{user.username if user.username else 'нет'}</code>\n"
                    f"• <b>ID:</b> <code>{user_id}</code>"
                ),
                parse_mode="HTML",
                reply_markup=admin_kb.as_markup()
            )
            return False
        except Exception as e:
            logging.error(f"Ошибка отправки запроса доступа: {e}", exc_info=True)
            return False

    async def check_access(message: types.Message) -> bool:
        return await check_access_by_user(message.from_user, bot)

    dp.workflow_data.update({
        "SHM_DIR": str(shm_dir),
        "user_mgr": user_mgr,
        "check_access": check_access,
        "check_access_by_user": check_access_by_user,
        "get_settings_keyboard": get_settings_keyboard,
        "http_session": http_session,
        "bot": bot,
        "ADMIN_ID": admin_id
    })

    dp.include_router(router)
    return bot, dp, user_mgr, http_session


# ==========================================
# 5. ГЛАВНАЯ ФУНКЦИЯ
# ==========================================

async def main():
    logging.info("Запуск PPTX2PNG Telegram Bot...")

    script_dir, env_name, bot_token, admin_id, shm_dir, log_dir = setup_environment()
    setup_logging(log_dir)

    logging.info(f"Окружение: {env_name}")
    logging.info(f"RAM-диск: {shm_dir}")
    logging.info(f"Логи: {log_dir}")

    # ✅ Безопасная очистка старых папок (не удаляет активные)
    cleanup_old_tasks(shm_dir, max_age_seconds=1800)

    bot, dp, user_mgr, http_session = create_bot_and_dispatcher(bot_token, admin_id, shm_dir, script_dir)

    asyncio.create_task(task_lock_manager.cleanup_loop(interval=600, max_age=7200))

    logging.info("✅ Бот успешно инициализирован и готов к работе")

    try:
        await dp.start_polling(bot)
    except asyncio.CancelledError:
        logging.info("⏹️ Поллинг остановлен по запросу")
        raise
    except KeyboardInterrupt:
        logging.info("⏹️ Бот остановлен пользователем")
        raise
    except Exception as e:
        logging.error(f"❌ Критическая ошибка в поллинге: {e}", exc_info=True)
        raise
    finally:
        await http_session.close()
        await bot.session.close()
        logging.info("✅ Бот завершил работу")


# ==========================================
# 6. ТОЧКА ВХОДА
# ==========================================

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("👋 Завершение работы по запросу пользователя")
        sys.exit(0)
    except Exception as e:
        logging.error(f"❌ Необработанная ошибка: {e}", exc_info=True)
        sys.exit(1)