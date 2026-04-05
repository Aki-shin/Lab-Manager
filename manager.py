import os
import sys
from app import create_app
from dotenv import load_dotenv

load_dotenv()

if not os.getenv('ADMIN_PASSWORD'):
    print("ОШИБКА: Переменная ADMIN_PASSWORD не задана в .env файле.")
    print("Создайте файл .env рядом с manager.py и добавьте строку:")
    print("  ADMIN_PASSWORD=ваш_надёжный_пароль")
    sys.exit(1)

app = create_app()

if __name__ == '__main__':
    port = int(os.getenv('MANAGER_PORT', 80))
    debug = os.getenv('FLASK_DEBUG', '0') == '1'
    # threaded=True нужен для SSE-стримов логов (параллельные соединения)
    app.run(host='0.0.0.0', port=port, debug=debug, threaded=True)