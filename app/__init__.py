from flask import Flask
from .config import Config


def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)

    # Уникальное имя cookie, чтобы не конфликтовать с внутренними Flask-приложениями,
    # которые тоже по умолчанию используют cookie с именем "session".
    app.config['SESSION_COOKIE_NAME'] = 'labmgr_session'
    app.config['SESSION_COOKIE_HTTPONLY'] = True
    app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
    app.config['SESSION_COOKIE_PATH'] = '/'

    Config.init_dirs()

    # Инициализация БД (users, permissions) + bootstrap admin
    from . import db
    db.init_db()

    # Регистрируем маршруты
    from .routes import bp
    app.register_blueprint(bp)

    # Контекст-процессор: current_user доступен во всех шаблонах
    from .auth import current_user

    @app.context_processor
    def inject_user():
        return {'current_user': current_user()}

    # Запускаем TCP-форвардеры для приложений с настроенным внешним портом
    from . import port_forwarder
    port_forwarder.init(app)

    # Фоновая автопроверка обновлений (панели и приложений)
    from . import update_checker
    update_checker.init(app)

    # WebSocket-терминал tmux
    from . import terminal
    terminal.init(app)

    return app