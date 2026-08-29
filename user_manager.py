from pathlib import Path

class UserManager:
    def __init__(self, admin_id: int, base_dir: Path):
        self.admin_id = admin_id
        # Теперь файл whitelist жестко привязан к каталогу скрипта (prod или test)
        self.white_list_file = base_dir / "allowed_users.txt"
        self.user_settings = {}  # Кэш настроек в RAM: {user_id: {"quality": "2k", "keep_pdf": False}}

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
            self.user_settings[user_id] = {"quality": "2k", "keep_pdf": False}
        return self.user_settings[user_id]
