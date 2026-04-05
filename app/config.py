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
    DB_PATH = os.getenv('DB_PATH', os.path.join(BASE_DIR, 'data', 'lab-manager.db'))

    @staticmethod
    def init_dirs():
        """Создаём необходимые директории."""
        os.makedirs(Config.APPS_DIR, exist_ok=True)
        os.makedirs(os.path.dirname(Config.DB_PATH), exist_ok=True)