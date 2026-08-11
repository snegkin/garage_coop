"""
Инициализация БД и создание первой учётной записи председателя.
Запуск: python seed.py
"""
from werkzeug.security import generate_password_hash

from app import create_app, database
from app.models import User, RoleEnum, Cooperative, FeeType


def main():
    app = create_app()  # уже создаёт таблицы через Base.metadata.create_all

    with app.app_context():
        if database.db_session.query(User).filter_by(username="chairman").first():
            print("Пользователь 'chairman' уже существует — пропускаю.")
        else:
            user = User(
                username="chairman",
                password_hash=generate_password_hash("change-me-please"),
                role=RoleEnum.CHAIRMAN,
                is_active=True,
            )
            database.db_session.add(user)
            print("Создан пользователь: chairman / change-me-please  (смените пароль после первого входа!)")

        if not database.db_session.query(Cooperative).first():
            database.db_session.add(Cooperative(full_name="", inn="", kpp="", ogrn=""))
            print("Создана пустая карточка кооператива — заполните реквизиты в разделе «Реквизиты».")

        # Базовые виды взносов. land_tax и membership имеют type_code — при
        # добавлении собственника гаражу для них автоматически заводится
        # персональный лицевой счёт (см. accounting.py и garages._ensure_member_accounts).
        # electricity — исключение: у него отдельный лицевой счёт на гараж (PersonalAccount).
        default_fee_types = [
            dict(code="electricity", name="Электричество", type_code=None, is_penalty=False),
            dict(code="land_tax", name="Земельный налог", type_code="1", is_penalty=False),
            dict(code="land_tax_penalty", name="Пеня по земельному налогу", type_code="1", is_penalty=True),
            dict(code="membership", name="Членский взнос", type_code="2", is_penalty=False),
            dict(code="membership_penalty", name="Пеня по членскому взносу", type_code="2", is_penalty=True),
            dict(code="target", name="Целевой взнос", type_code=None, is_penalty=False),
        ]
        for ft in default_fee_types:
            if not database.db_session.query(FeeType).filter_by(code=ft["code"]).first():
                database.db_session.add(FeeType(**ft))
                print(f"Добавлен вид взноса: {ft['name']}")

        database.db_session.commit()


if __name__ == "__main__":
    main()
