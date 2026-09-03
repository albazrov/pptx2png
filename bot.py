import sys
import os
import logging
import asyncio
import shutil
import configparser
import argparse
from pathlib import Path 

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart
from aiogram.types import FSInputFile, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
import aiohttp 

# ИМПОРТ НАШИХ КАСТОМНЫХ МОДУЛЕЙ
import converter_engine
from user_manager import UserManager
from utils import extract_text_from_pptx, check_spelling
from handlers import router

# ==========================================
# 1. ОПРЕДЕЛЕНИЕ КОНФИГУРАЦИИ И ПУТЕЙ
# ==========================================

def setup_environment():
    """Настройка окружения и путей."""
    # Определение директории запуска скрипта и имени родительской папки
    script_dir = os.path.dirname(os.path.abspath(__file__))
    env_name = os.path.basename(script_dir)

    # Настраиваем парсер аргументов командной строки
    parser = argparse.ArgumentParser(description="PPTX2PNG Telegram Bot")
    parser.add_argument("--log-dir", type=str, help="Путь к папке логов (по дефолту внутри SHM)")
    parser.add_argument("--shm-dir", type=str, help="Путь к временной папке в RAM-диске")
    args, unknown = parser.parse_known_args()

    # Инициализация конфигураций .ini относительно SCRIPT_DIR
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

    # Приоритет путей для RAM-диска (SHM)
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

    # Приоритет путей для логов
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


def setup_logging(log_dir: str):
    """Настройка логирования."""
    log_formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)

    # Основной файл логов (INFO и выше)
    info_handler = logging.FileHandler(os.path.join(log_dir, "bot.log"), encoding='utf-8')
    info_handler.setLevel(logging.INFO)
    info_handler.setFormatter(log_formatter)
    root_logger.addHandler(info_handler)

    # Детальный дебаг файл (DEBUG и выше)
    debug_handler = logging.FileHandler(os.path.join(log_dir, "debug.log"), encoding='utf-8')
    debug_handler.setLevel(logging.DEBUG)
    debug_handler.setFormatter(log_formatter)
    root_logger.addHandler(debug_handler)

    # Вывод в консоль (INFO и выше)
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setLevel(logging.INFO)
    stdout_handler.setFormatter(log_formatter)
    root_logger.addHandler(stdout_handler)


def escape_markdown(text: str) -> str:
    """Экранирует специальные символы Markdown V2 в тексте."""
    special_chars = r'_*[]()~`>#+-=|{}.!'
    for char in special_chars:
        text = text.replace(char, f'\\{char}')
    return text


# ==========================================
# 2. СОЗДАНИЕ БОТА И ДИСПЕТЧЕРА
# ==========================================

def create_bot_and_dispatcher(bot_token: str, admin_id: int, shm_dir: Path, script_dir: str):
    """Создаёт экземпляры бота и диспетчера с настройками."""
    bot = Bot(token=bot_token)
    dp = Dispatcher()
    
    # Инициализация менеджера пользователей
    user_mgr = UserManager(admin_id=admin_id, base_dir=Path(script_dir))
    
    # Создаём клавиатуру настроек
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
    
    # Функция проверки доступа для пользователя
    async def check_access_by_user(user: types.User, bot: Bot) -> bool:
        user_id = user.id
        
        if user_id in user_mgr.load_allowed_users():
            return True
        
        admin_kb = InlineKeyboardBuilder()
        admin_kb.row(
            InlineKeyboardButton(text="✅ Разрешить", callback_data=f"adm_allow_{user_id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"adm_deny_{user_id}")
        )
        
        safe_full_name = escape_markdown(user.full_name or "без имени")
        safe_username = escape_markdown(f"@{user.username}") if user.username else "нет юзернейма"
        
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
            logging.error(f"Ошибка отправки запроса доступа админу: {e}", exc_info=True)
            return False
    
    async def check_access(message: types.Message) -> bool:
        return await check_access_by_user(message.from_user, bot)
    
    # Регистрируем зависимости в диспетчере
    dp.workflow_data.update({
        "SHM_DIR": str(shm_dir),
        "user_mgr": user_mgr,
        "check_access": check_access,
        "check_access_by_user": check_access_by_user,
        "get_settings_keyboard": get_settings_keyboard,
        "bot": bot,
        "ADMIN_ID": admin_id
    })
    
    # Подключаем роутер с хендлерами
    dp.include_router(router)
    
    return bot, dp, user_mgr


# ==========================================
# 3. ГЛАВНАЯ ФУНКЦИЯ ЗАПУСКА
# ==========================================

async def main():
    """Основная функция запуска Telegram-бота."""
    logging.info("Запуск PPTX2PNG Telegram Bot...")
    
    # 1. Настройка окружения
    script_dir, env_name, bot_token, admin_id, shm_dir, log_dir = setup_environment()
    
    # 2. Настройка логирования
    setup_logging(log_dir)
    
    logging.info(f"Окружение: {env_name}")
    logging.info(f"RAM-диск: {shm_dir}")
    logging.info(f"Логи: {log_dir}")
    
    # 3. Создание бота и диспетчера
    bot, dp, user_mgr = create_bot_and_dispatcher(bot_token, admin_id, shm_dir, script_dir)
    
    logging.info("✅ Бот успешно инициализирован и готов к работе")
    
    # 4. Запуск поллинга с корректной обработкой прерываний
    try:
        await dp.start_polling(bot)
    except asyncio.CancelledError:
        logging.info("⏹️ Поллинг остановлен по запросу")
        raise
    except KeyboardInterrupt:
        logging.info("⏹️ Бот остановлен пользователем (KeyboardInterrupt)")
        raise
    except Exception as e:
        logging.error(f"❌ Критическая ошибка в поллинге: {e}", exc_info=True)
        raise
    finally:
        # Корректное завершение
        logging.info("🧹 Выполняется очистка ресурсов...")
        try:
            await bot.session.close()
        except Exception as e:
            logging.error(f"Ошибка при закрытии сессии: {e}")
        logging.info("✅ Бот завершил работу")


# ==========================================
# 4. ТОЧКА ВХОДА
# ==========================================

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        # Пользователь нажал Ctrl+C
        logging.info("👋 Завершение работы по запросу пользователя")
        sys.exit(0)
    except Exception as e:
        logging.error(f"❌ Необработанная ошибка: {e}", exc_info=True)
        sys.exit(1)
        
