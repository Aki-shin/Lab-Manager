from flask import Flask
from .config import Config


def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)
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

    return app