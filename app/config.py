import os
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Config:
    SECRET_KEY = os.getenv('SECRET_KEY', 'dev_key')
    ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD')
    APPS_DIR = os.getenv('APPS_DIR', '/root/apps')
    SYSTEMD_DIR = '/etc/systemd/system'
    SERVICE_PREFIX = 'labapp-'
    # host-manager.db по умолчанию; если от старой версии остался
    # lab-manager.db, а нового файла ещё нет — используем старый,
    # чтобы не потерять пользователей и настройки.
    _DEFAULT_DB = os.path.join(BASE_DIR, 'data', 'host-manager.db')
    _LEGACY_DB = os.path.join(BASE_DIR, 'data', 'lab-manager.db')
    DB_PATH = os.getenv('DB_PATH') or (
        _LEGACY_DB if (os.path.exists(_LEGACY_DB)
                       and not os.path.exists(_DEFAULT_DB))
        else _DEFAULT_DB
    )

    @staticmethod
    def init_dirs():
        """Создаём необходимые директории."""
        os.makedirs(Config.APPS_DIR, exist_ok=True)
        os.makedirs(os.path.dirname(Config.DB_PATH), exist_ok=True)