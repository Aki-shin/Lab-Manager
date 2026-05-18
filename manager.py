import os
import sys
import signal
from app import create_app
from dotenv import load_dotenv

load_dotenv()

if not os.getenv('ADMIN_PASSWORD'):
    print("ОШИБКА: Переменная ADMIN_PASSWORD не задана в .env файле.")
    print("Создайте файл .env рядом с manager.py и добавьте строку:")
    print("  ADMIN_PASSWORD=ваш_надёжный_пароль")
    sys.exit(1)

app = create_app()

def _terminate(*_):
    """
    systemd шлёт SIGTERM при stop/restart. Werkzeug dev-server с threaded=True
    и фоновыми потоками (SSE-логи, форвардеры) не всегда завершается по нему
    сам — из-за этого `systemctl stop` висит до TimeoutStopSec, процесс
    продолжает держать порт, а самообновление «не перезапускается».
    Завершаемся немедленно и принудительно.
    """
    os._exit(0)


if __name__ == '__main__':
    port = int(os.getenv('MANAGER_PORT', 80))
    debug = os.getenv('FLASK_DEBUG', '0') == '1'

    # Корректное завершение по сигналам systemd
    signal.signal(signal.SIGTERM, _terminate)
    signal.signal(signal.SIGINT, _terminate)

    # threaded=True нужен для SSE-стримов логов (параллельные соединения)
    app.run(host='0.0.0.0', port=port, debug=debug, threaded=True)