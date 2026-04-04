import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    SECRET_KEY = os.getenv('SECRET_KEY', 'dev_key')
    ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD')
    USER_HOME = os.path.expanduser("~")
    APPS_DIR = os.path.join(USER_HOME, "apps")
    SYSTEMD_USER_DIR = os.path.join(USER_HOME, ".config/systemd/user")
    
    # Создаем директории, если их нет
    if not os.path.exists(SYSTEMD_USER_DIR):
        os.makedirs(SYSTEMD_USER_DIR)