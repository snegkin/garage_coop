import os

from flask import Flask, g, redirect, url_for

from config import Config
from .database import init_engine, init_app as init_db_lifecycle
from .models import Base


def create_app(config_class=Config):
    app = Flask(__name__)
    app.config.from_object(config_class)

    os.makedirs(os.path.dirname(app.config["SQLALCHEMY_DATABASE_URI"].replace("sqlite:///", "")), exist_ok=True)
    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)

    engine, _db_session = init_engine(app.config["SQLALCHEMY_DATABASE_URI"])
    Base.metadata.create_all(engine)  # для SQLite достаточно; для миграций позже можно подключить Alembic
    init_db_lifecycle(app)

    from . import auth, i18n, theme
    app.register_blueprint(auth.bp)
    i18n.init_app(app)
    theme.init_app(app)

    @app.before_request
    def _load_user():
        auth.load_logged_in_user()

    @app.context_processor
    def _inject_user():
        from . import database
        from .models import Cooperative
        coop = database.db_session.query(Cooperative).first()
        coop_name = (coop.short_name or coop.full_name) if coop and (coop.short_name or coop.full_name) else "ГСК"
        return {"current_user": g.get("user"), "coop_name": coop_name}

    from .main import bp as main_bp
    from .garages import bp as garages_bp
    from .persons import bp as persons_bp
    from .finance import bp as finance_bp
    from .documents import bp as documents_bp
    from .meetings import bp as meetings_bp
    from .cooperative import bp as cooperative_bp
    from .electricity import bp as electricity_bp
    from .cabinet import bp as cabinet_bp
    from .power import bp as power_bp
    from .counterparties import bp as counterparties_bp
    from .pd4 import bp as pd4_bp

    app.register_blueprint(main_bp)
    app.register_blueprint(garages_bp)
    app.register_blueprint(persons_bp)
    app.register_blueprint(finance_bp)
    app.register_blueprint(documents_bp)
    app.register_blueprint(meetings_bp)
    app.register_blueprint(cooperative_bp)
    app.register_blueprint(electricity_bp)
    app.register_blueprint(cabinet_bp)
    app.register_blueprint(power_bp)
    app.register_blueprint(counterparties_bp)
    app.register_blueprint(pd4_bp)

    @app.route("/")
    def index():
        return redirect(url_for("main.dashboard"))

    return app
