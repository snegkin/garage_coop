import datetime as dt
import os
import logging

from flask import Flask, g, redirect, url_for
from flask_wtf import CSRFProtect

from config import Config
from .database import init_engine, run_migrations, init_app as init_db_lifecycle
from .rate_limit import limiter

csrf = CSRFProtect()


def create_app(config_class=Config):
    app = Flask(__name__)
    app.config.from_object(config_class)
    csrf.init_app(app)
    limiter.init_app(app)

    if not app.testing:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )

    from .errors import register_error_handlers
    register_error_handlers(app)

    os.makedirs(os.path.dirname(app.config["SQLALCHEMY_DATABASE_URI"].replace("sqlite:///", "")), exist_ok=True)
    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
    # 0o700: только сам процесс приложения может читать/писать сюда — здесь
    # лежат приватные ключи клиентских mTLS-сертификатов банка (см.
    # config.py: BANK_CERTS_FOLDER, app/bank_api/sberbank.py). В отличие от
    # UPLOAD_FOLDER, эта директория не имеет отдающего файлы HTTP-роута.
    os.makedirs(app.config["BANK_CERTS_FOLDER"], exist_ok=True, mode=0o700)
    os.chmod(app.config["BANK_CERTS_FOLDER"], 0o700)  # makedirs(mode=...) не применяет режим, если каталог уже существовал

    run_migrations(app.config["SQLALCHEMY_DATABASE_URI"])  # накатывает схему через Alembic, не трогая данные
    engine, _db_session = init_engine(app.config["SQLALCHEMY_DATABASE_URI"])
    init_db_lifecycle(app)

    from . import auth, i18n, theme, news_format, contact_format, comment_format
    app.register_blueprint(auth.bp)
    i18n.init_app(app)
    theme.init_app(app)
    app.jinja_env.globals["render_news_html"] = news_format.render_html
    app.jinja_env.globals["news_excerpt"] = news_format.excerpt
    # Вики использует тот же markdown-рендер, что и новости (news_format.py
    # не завязан на модель News) — отдельное имя jinja-глобала для ясности
    # в шаблонах wiki/*.html, функция та же самая.
    app.jinja_env.globals["render_wiki_html"] = news_format.render_html
    app.jinja_env.globals["phone_link"] = contact_format.phone_link
    app.jinja_env.globals["phones_html"] = contact_format.phones_html
    app.jinja_env.globals["email_link"] = contact_format.email_link
    app.jinja_env.globals["telegram_link"] = contact_format.telegram_link
    app.jinja_env.globals["linkify_related_person"] = comment_format.linkify_related_person

    @app.before_request
    def _load_user():
        auth.load_logged_in_user()

    @app.context_processor
    def _inject_user():
        from . import database
        from .models import (
            Cooperative, PersonDataRevision, PersonDataRevisionStatus, Vote, VoteQuestion, VoteBallot, VoteStatus,
            RoleEnum, Person, VoteProposal, VoteProposalBoardBallot, ProposalStatus,
        )
        from .accounting import balance as _balance
        from .permissions import is_board, is_chairman, is_privileged
        coop = database.db_session.query(Cooperative).first()
        coop_name = (coop.short_name or coop.full_name) if coop and (coop.short_name or coop.full_name) else "ГСК"

        # уведомления для председателя — ожидающие одобрения изменений ПД
        pending_pd = 0
        user = g.get("user")
        if user and user.role == RoleEnum.CHAIRMAN:
            pending_pd = database.db_session.query(PersonDataRevision).filter(
                PersonDataRevision.status == PersonDataRevisionStatus.PENDING
            ).count()

        # уведомления для всех — открытые голосования без бюллетеня пользователя
        pending_votes = 0
        if user and user.person_id:
            person = database.db_session.get(Person, user.person_id)
            if person:
                # открытые голосования, где у пользователя нет ни одного бюллетеня
                pending_votes = database.db_session.query(Vote).filter(
                    Vote.status == VoteStatus.OPEN,
                    Vote.closes_at > dt.datetime.utcnow(),
                    ~database.db_session.query(VoteQuestion.id)
                    .join(VoteBallot)
                    .filter(
                        VoteQuestion.vote_id == Vote.id,
                        VoteBallot.person_id == person.id,
                    ).exists(),
                ).count()

        # уведомления для правления — предложения членов кооператива, ждущие голоса этого человека
        pending_proposals = 0
        if user and user.person_id:
            from .governance import current_board_member_ids
            if user.person_id in current_board_member_ids():
                pending_proposals = database.db_session.query(VoteProposal).filter(
                    VoteProposal.status == ProposalStatus.PENDING,
                    ~database.db_session.query(VoteProposalBoardBallot.id).filter(
                        VoteProposalBoardBallot.proposal_id == VoteProposal.id,
                        VoteProposalBoardBallot.person_id == user.person_id,
                    ).exists(),
                ).count()

        return {
            "current_user": user, "coop_name": coop_name, "balance": _balance,
            "is_board": is_board, "is_chairman": is_chairman, "is_privileged": is_privileged,
            "pending_pd_count": pending_pd,
            "pending_votes_count": pending_votes,
            "pending_proposals_count": pending_proposals,
        }

    from .main import bp as main_bp
    from .garages import bp as garages_bp
    from .persons import bp as persons_bp
    from .finance import bp as finance_bp

    from .meetings import bp as meetings_bp
    from .cooperative import bp as cooperative_bp
    from .bank_sync import bp as bank_sync_bp
    from .cabinet import bp as cabinet_bp
    from .power import bp as power_bp
    from .electricity_monitor import bp as electricity_monitor_bp
    from .counterparties import bp as counterparties_bp
    from .pd4 import bp as pd4_bp
    from .governance import bp as governance_bp
    from .penalty import bp as penalty_bp
    from .voting import bp as voting_bp
    from .proposals import bp as proposals_bp
    from .news import bp as news_bp
    from .wiki import bp as wiki_bp
    from .setup_wizard import bp as setup_wizard_bp

    app.register_blueprint(main_bp)
    app.register_blueprint(garages_bp)
    app.register_blueprint(persons_bp)
    app.register_blueprint(finance_bp)

    app.register_blueprint(meetings_bp)
    app.register_blueprint(cooperative_bp)
    app.register_blueprint(bank_sync_bp)
    app.register_blueprint(cabinet_bp)
    app.register_blueprint(power_bp)
    app.register_blueprint(electricity_monitor_bp)
    app.register_blueprint(counterparties_bp)
    app.register_blueprint(pd4_bp)
    app.register_blueprint(governance_bp)
    app.register_blueprint(penalty_bp)
    app.register_blueprint(voting_bp)
    app.register_blueprint(proposals_bp)
    app.register_blueprint(news_bp)
    app.register_blueprint(wiki_bp)
    app.register_blueprint(setup_wizard_bp)

    @app.route("/")
    def index():
        return redirect(url_for("main.dashboard"))

    return app
