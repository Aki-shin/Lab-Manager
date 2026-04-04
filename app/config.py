import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    SECRET_KEY = os.getenv('SECRET_KEY', 'dev_key')
    ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD')
    APPS_DIR = os.getenv('APPS_DIR', '/root/apps')
    SYSTEMD_DIR = '/etc/systemd/system'
    SERVICE_PREFIX = 'labapp-'

    @staticmethod
    def init_dirs():
        """Создаём необходимые директории."""
        os.makedirs(Config.APPS_DIR, exist_ok=True)