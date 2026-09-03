import json
from pathlib import Path
import logging

# 20260828

class UserManager:
    def __init__(self, admin_id: int, base_dir: Path):
        self.admin_id = admin_id
        # Защита от Path.cwd() по рекомендации Qodo
        self.white_list_file = base_dir / "allowed_users.txt"
        self.settings_file = base_dir / "user_settings.json"
        self.user_settings = {}  # Кэш настроек пользователей в RAM
        
        # Автоматически восстанавливаем настройки при запуске процесса
        self._load_settings()

    def _load_settings(self):
        """Внутренний метод для чтения JSON-файла настроек."""
        if self.settings_file.exists():
            try:
                with open(self.settings_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    # JSON превращает ключи-числа в строки ("999999"), 
                    # принудительно возвращаем их в int для корректного маппинга aiogram
                    self.user_settings = {int(k): v for k, v in data.items()}
                logging.info(f"💾 Настройки пользователей успешно загружены из {self.settings_file.name}")
            except Exception as e:
                logging.error(f"❌ Ошибка чтения файла настроек JSON: {e}")
                self.user_settings = {}

    def _save_settings(self):
        """Внутренний метод для записи актуальных настроек в JSON-файл."""
        try:
            with open(self.settings_file, "w", encoding="utf-8") as f:
                json.dump(self.user_settings, f, ensure_ascii=False, indent=4)
        except Exception as e:
            logging.error(f"❌ Ошибка записи файла настроек JSON: {e}")

    def load_allowed_users(self) -> set:
        """Загружает список разрешенных ID из файла."""
        users = {self.admin_id}  # Админ всегда имеет доступ
        if self.white_list_file.exists():
            with open(self.white_list_file, "r") as f:
                for line in f:
                    line = line.strip()
                    if line.isdigit():
                        users.add(int(line))
        return users

    def save_allowed_user(self, user_id: int):
        """Добавляет нового пользователя в белый список."""
        users = self.load_allowed_users()
        if user_id not in users:
            with open(self.white_list_file, "a") as f:
                f.write(f"{user_id}\n")

    def get_user_config(self, user_id: int) -> dict:
        """Возвращает настройки генерации для конкретного пользователя."""
        if user_id not in self.user_settings:
            # Создаем стандартные дефолты для нового ID
            self.user_settings[user_id] = {"quality": "2k", "keep_pdf": False}
            self._save_settings()
        return self.user_settings[user_id]

    def update_user_config(self, user_id: int, key: str, value):
        """Обновляет параметр пользователя и сразу перезаписывает файл на диске."""
        if user_id not in self.user_settings:
            self.user_settings[user_id] = {"quality": "2k", "keep_pdf": False}
        
        self.user_settings[user_id][key] = value
        self._save_settings()  # Данные мгновенно защищены от перезапуска manage.sh
