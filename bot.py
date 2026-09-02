# ==========================================
# bot.py — ГЛАВНЫЙ ЗАПУСКНОЙ СКРИПТ
# ==========================================

import sys
import os
import logging
import asyncio
import shutil
import configparser
import argparse
import time
from pathlib import Path
from logging.handlers import RotatingFileHandler

from aiogram import Bot, Dispatcher, types
from aiogram.types import InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
import aiohttp

# Импорты из внутренних модулей проекта
try:
    from user_manager import UserManager
    from handlers import router, session_manager, task_scheduler
except ImportError:
    pass


def setup_environment():
    """Настройка путей, конфигов и параметров производительности."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    env_name = os.path.basename(script_dir)

    parser = argparse.ArgumentParser(description="PPTX2PNG Telegram Bot")
    parser.add_argument("--log-dir", type=str, help="Путь к папке логов")
    parser.add_argument("--shm-dir", type=str, help="Путь к RAM-диску")
    # Параметры контроля нагрузки
    parser.add_argument("--max-concurrency", type=int, default=3, help="Максимум одновременных конвертаций")
    parser.add_argument("--max-per-user", type=int, default=1, help="Максимум задач на одного пользователя")
    
    args, unknown = parser.parse_known_args()

    config_path = Path(script_dir) / "config.ini"
    settings_path = Path(script_dir) / "settings.ini"

    if not config_path.exists():
        sys.exit(f"❌ Ошибка: config.ini не найден по пути: {config_path}")
    
    config = configparser.ConfigParser()
    config.read(config_path, encoding='utf-8')

    try:
        bot_token = config.get("Telegram", "BOT_TOKEN").strip()
        admin_id = int(config.get("Telegram", "ADMIN_ID").strip())
    except Exception as e:
        sys.exit(f"❌ Ошибка в config.ini: {e}")

    # Путь к SHM
    if args.shm_dir:
        shm_dir = Path(args.shm_dir)
    else:
        try:
            base_shm = settings_config.get("Paths", "shm_dir").strip()
            shm_dir = Path(base_shm) / env_name
        except Exception:
            shm_dir = Path("/dev/shm/pptx2png_tasks") / env_name
            
    shm_dir.mkdir(parents=True, exist_ok=True)

    # Путь к Логам
    if args.log_dir:
        log_dir = args.log_dir
    else:
        try:
            base_log = settings_config.get("Paths", "log_dir").strip()
            log_dir = base_log
        except Exception:
            log_dir = os.path.join(str(shm_dir), "logs")
            
    os.makedirs(log_dir, exist_ok=True)

    return script_dir, env_name, bot_token, admin_id, shm_dir, log_dir, args.max_concurrency, args.max_per_user


def setup_logging(log_dir: str):
    """Настройка ротации логов."""
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s - %(message)s")
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)

    # Info Log
    fh_info = RotatingFileHandler(os.path.join(log_dir, "bot.log"), maxBytes=10*1024*1024, backupCount=5, encoding='utf-8')
    fh_info.setFormatter(fmt)
    fh_info.setLevel(logging.INFO)
    root_logger.addHandler(fh_info)

    # Debug Log
    fh_debug = RotatingFileHandler(os.path.join(log_dir, "debug.log"), maxBytes=10*1024*1024, backupCount=3, encoding='utf-8')
    fh_debug.setFormatter(fmt)
    fh_debug.setLevel(logging.DEBUG)
    root_logger.addHandler(fh_debug)

    # Console
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    sh.setLevel(logging.INFO)
    root_logger.addHandler(sh)


async def cleanup_loop(shm_dir: Path, interval: int = 600, max_age_seconds: int = 3600):
    """Фоновый процесс очистки старых задач."""
    while True:
        await asyncio.sleep(interval)
        try:
            deleted = await asyncio.to_thread(cleanup_old_tasks_sync, shm_dir, max_age_seconds)
            if deleted > 0:
                logging.info(f"🧹 Cleanup: удалено {deleted} старых папок.")
        except Exception as e:
            logging.error(f"Ошибка фоновой очистки: {e}", exc_info=True)


def cleanup_old_tasks_sync(shm_dir: Path, max_age_seconds: int):
    """Синхронная функция для вызова через to_thread."""
    if not shm_dir.exists(): return 0
    
    current_time = time.time()
    deleted = 0
    
    for item in shm_dir.iterdir():
        if item.is_dir() and item.name.startswith("task_"):
            try:
                age = current_time - item.stat().st_mtime
                if age > max_age_seconds:
                    shutil.rmtree(item)
                    deleted += 1
            except Exception:
                pass
    return deleted


def create_bot_and_dispatcher(bot_token: str, admin_id: int, shm_dir: Path, script_dir: str, max_concurrency: int, max_per_user: int):
    bot = Bot(token=bot_token)
    dp = Dispatcher()

    # Инициализация менеджера пользователей
    user_mgr = UserManager(admin_id=admin_id, base_dir=Path(script_dir))
    http_session = aiohttp.ClientSession()

    # Инициализация планировщика ресурсов
    global task_scheduler
    task_scheduler = TaskScheduler(max_global=max_concurrency, max_per_user=max_per_user)

    def get_settings_keyboard(user_id):
        cfg = user_mgr.get_user_config(user_id)
        q = cfg.get("quality", "standard")
        pdf_status = cfg.get("keep_pdf", False)
        
        builder = InlineKeyboardBuilder()
        builder.row(
            InlineKeyboardButton(text="✅ Standard" if q=="standard" else "Standard", callback_data="set_q_standard"),
            InlineKeyboardButton(text="✅ 2K" if q=="2k" else "2K", callback_data="set_q_2k"),
            InlineKeyboardButton(text="✅ 4K" if q=="4k" else "4K", callback_data="set_q_4k")
        )
        builder.row(InlineKeyboardButton(text=f"PDF: {'Да' if pdf_status else 'Нет'}", callback_data="toggle_pdf"))
        return builder.as_markup()

    async def check_access_by_user(user: types.User, bot: Bot) -> bool:
        if user.id in user_mgr.load_allowed_users():
            return True
        
        admin_kb = InlineKeyboardBuilder()
        admin_kb.row(
            InlineKeyboardButton(text="✅ Да", callback_data=f"adm_allow_{user.id}"),
            InlineKeyboardButton(text="❌ Нет", callback_data=f"adm_deny_{user.id}")
        )
        try:
            await bot.send_message(
                chat_id=admin_id,
                text=(f"🔔 <b>Запрос доступа!</b>\n• ID: <code>{user.id}</code>\n• Name: <code>{user.full_name or 'N/A'}</code>"),
                parse_mode="HTML", reply_markup=admin_kb.as_markup()
            )
            return False
        except Exception as e:
            logging.error(f"Admin notify error: {e}")
            return False

    async def check_access(message: types.Message) -> bool:
        return await check_access_by_user(message.from_user, bot)

    # Передача данных в воркфлоу
    dp.workflow_data.update({
        "SHM_DIR": str(shm_dir),
        "user_mgr": user_mgr,
        "check_access": check_access,
        "check_access_by_user": check_access_by_user,
        "get_settings_keyboard": get_settings_keyboard,
        "bot": bot,
        "admin_id": admin_id,
        "task_scheduler": task_scheduler # Добавляем планировщик
    })

    dp.include_router(router)
    return bot, dp, user_mgr, http_session


async def main():
    logging.info("🚀 Запуск PPTX2PNG Bot...")

    script_dir, env_name, bot_token, admin_id, shm_dir, log_dir, max_conc, max_user = setup_environment()
    setup_logging(log_dir)

    logging.info(f"Окружение: {env_name}, SHM: {shm_dir}")
    logging.info(f"Лимиты: Глобально={max_conc}, На юзера={max_user}")

    # Полная очистка при старте (если это критично для вашего случая, иначе можно убрать)
    # cleanup_old_tasks_sync(shm_dir, 0) 

    bot, dp, user_mgr, http_session = create_bot_and_dispatcher(
        bot_token, admin_id, shm_dir, script_dir, 
        max_concurrent=max_conc, max_per_user=max_user
    )

    # Запуск фонового сборщика мусора
    asyncio.create_task(cleanup_loop(shm_dir, interval=600, max_age_seconds=3600))

    logging.info("✅ Бот готов.")
    try:
        await dp.start_polling(bot)
    except KeyboardInterrupt:
        logging.info("⏹️ Пользователь остановил бота")
    except Exception as e:
        logging.critical(f"💥 Критическая ошибка поллинга: {e}", exc_info=True)
    finally:
        await http_session.close()
        await bot.session.close()
        logging.info("✅ Работа завершена.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        logging.critical(f"Fatal Error: {e}", exc_info=True)
        sys.exit(1)
