# ==========================================
# bot.py — ГЛАВНЫЙ ФАЙЛ (REFACTORED)
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

# Импорты
try:
    from user_manager import UserManager
    from handlers import router, session_manager
except ImportError:
    pass


def setup_environment():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    env_name = os.path.basename(script_dir)

    parser = argparse.ArgumentParser(description="PPTX2PNG Telegram Bot")
    parser.add_argument("--log-dir", type=str, help="Путь к папке логов")
    parser.add_argument("--shm-dir", type=str, help="Путь к RAM-диску")
    args, unknown = parser.parse_known_args()

    config_path = Path(script_dir) / "config.ini"
    settings_path = Path(script_dir) / "settings.ini"

    if not config_path.exists():
        sys.exit(f"❌ config.ini не найден: {config_path}")
    
    config = configparser.ConfigParser()
    config.read(config_path, encoding='utf-8')

    try:
        bot_token = config.get("Telegram", "BOT_TOKEN").strip()
        admin_id = int(config.get("Telegram", "ADMIN_ID").strip())
    except Exception as e:
        sys.exit(f"❌ Ошибка конфига: {e}")

    shm_dir = Path(args.shm_dir) if args.shm_dir else Path("/dev/shm/pptx2png_tasks") / env_name
    shm_dir.mkdir(parents=True, exist_ok=True)

    log_dir = args.log_dir if args.log_dir else os.path.join(str(shm_dir), "logs")
    os.makedirs(log_dir, exist_ok=True)

    return script_dir, env_name, bot_token, admin_id, shm_dir, log_dir


def setup_logging(log_dir: str):
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    # Info Log
    fh_info = RotatingFileHandler(os.path.join(log_dir, "bot.log"), maxBytes=10*1024*1024, backupCount=5)
    fh_info.setFormatter(fmt)
    fh_info.setLevel(logging.INFO)
    root.addHandler(fh_info)

    # Debug Log
    fh_debug = RotatingFileHandler(os.path.join(log_dir, "debug.log"), maxBytes=10*1024*1024, backupCount=3)
    fh_debug.setFormatter(fmt)
    fh_debug.setLevel(logging.DEBUG)
    root.addHandler(fh_debug)

    # Console
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    sh.setLevel(logging.INFO)
    root.addHandler(sh)


def cleanup_old_tasks(shm_dir: Path, max_age_seconds: int = 3600):
    """Фоновая очистка старых задач."""
    if not shm_dir.exists(): return
    
    deleted = 0
    current_time = time.time()
    
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


async def cleanup_loop(shm_dir: Path, interval: int = 600, max_age: int = 3600):
    while True:
        await asyncio.sleep(interval)
        try:
            del_count = await asyncio.to_thread(cleanup_old_tasks, shm_dir, max_age)
            if del_count:
                logging.info(f"🧹 Cleanup: удалено {del_count} старых папок")
        except Exception as e:
            logging.error(f"Cleanup error: {e}")


def create_bot_and_dispatcher(bot_token: str, admin_id: int, shm_dir: Path, script_dir: str):
    bot = Bot(token=bot_token)
    dp = Dispatcher()

    user_mgr = UserManager(admin_id=admin_id, base_dir=Path(script_dir))
    http_session = aiohttp.ClientSession()

    def get_settings_keyboard(user_id):
        cfg = user_mgr.get_user_config(user_id)
        q = cfg.get("quality", "standard")
        pdf = cfg.get("keep_pdf", False)
        
        builder = InlineKeyboardBuilder()
        builder.row(
            InlineKeyboardButton(text="✅ Standard" if q=="standard" else "Standard", callback_data="set_q_standard"),
            InlineKeyboardButton(text="✅ 2K" if q=="2k" else "2K", callback_data="set_q_2k"),
            InlineKeyboardButton(text="✅ 4K" if q=="4k" else "4K", callback_data="set_q_4k")
        )
        builder.row(InlineKeyboardButton(text=f"PDF: {'Да' if pdf else 'Нет'}", callback_data="toggle_pdf"))
        return builder.as_markup()

    async def check_access_by_user(user: types.User, bot: Bot) -> bool:
        if user.id in user_mgr.load_allowed_users(): return True
        
        admin_kb = InlineKeyboardBuilder()
        admin_kb.row(
            InlineKeyboardButton(text="✅ Да", callback_data=f"adm_allow_{user.id}"),
            InlineKeyboardButton(text="❌ Нет", callback_data=f"adm_deny_{user.id}")
        )
        try:
            await bot.send_message(
                admin_id, 
                f"🔔 <b>Запрос доступа!</b>\nID: <code>{user.id}</code>", 
                parse_mode="HTML", 
                reply_markup=admin_kb.as_markup()
            )
            return False
        except Exception as e:
            logging.error(f"Admin notify error: {e}")
            return False

    async def check_access(message: types.Message) -> bool:
        return await check_access_by_user(message.from_user, bot)

    dp.workflow_data.update({
        "SHM_DIR": str(shm_dir),
        "user_mgr": user_mgr,
        "check_access": check_access,
        "check_access_by_user": check_access_by_user,
        "get_settings_keyboard": get_settings_keyboard,
        "bot": bot,
        "ADMIN_ID": admin_id
    })

    dp.include_router(router)
    return bot, dp, user_mgr, http_session


async def main():
    logging.info("🚀 Starting PPTX2PNG Bot...")
    
    script_dir, env_name, bot_token, admin_id, shm_dir, log_dir = setup_environment()
    setup_logging(log_dir)

    logging.info(f"Env: {env_name}, SHM: {shm_dir}")

    bot, dp, user_mgr, http_session = create_bot_and_dispatcher(bot_token, admin_id, shm_dir, script_dir)

    # Запуск фонового уборщика вместо агрессивного старта
    asyncio.create_task(cleanup_loop(shm_dir))

    logging.info("✅ Ready.")
    try:
        await dp.start_polling(bot)
    except KeyboardInterrupt:
        logging.info("Stopped.")
    finally:
        await http_session.close()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        logging.critical(f"FATAL: {e}", exc_info=True)
        sys.exit(1)
