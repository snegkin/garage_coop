"""
Порядок гаражей по номеру «по-человечески» (models.garage_number_key):
номер — строка, но 2 идёт раньше 10, а 61а — сразу за 61. Так упорядочены
список гаражей и навигация «предыдущий/следующий» на карточке (раньше —
по id). Плюс фильтр «на территории / вне территории» в списке.
"""
from app.models import RoleEnum, garage_number_key

from tests.conftest import make_garage, make_user, login


def test_garage_number_key():
    numbers = ["10", "2", "61а", "1н", "61", "1", "11", "Б/н"]
    assert sorted(numbers, key=garage_number_key) == ["1", "1н", "2", "10", "11", "61", "61а", "Б/н"]


def test_list_and_navigation_by_number(app, db, client):
    # Создаются не по порядку номеров — id с номером не совпадает.
    g11 = make_garage(db, number="11")
    g2 = make_garage(db, number="2")
    g10 = make_garage(db, number="10", outside_territory=True)
    g1 = make_garage(db, number="1")
    make_user(db, "boardord", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "boardord", "pass12345")

    body = client.get("/garages/").get_data(as_text=True)
    positions = [body.index(f"/garages/{g.id}'") for g in (g1, g2, g10, g11)]
    assert positions == sorted(positions)
    assert 'data-sort-default="asc"' in body
    # Фильтр на/вне территории — плашками сводки (data-groups, см. _stats.html).
    assert 'data-groups="outside' in body and 'data-groups="inside' in body

    page = client.get(f"/garages/{g10.id}").get_data(as_text=True)
    assert f"/garages/{g2.id}?" in page and f"/garages/{g11.id}?" in page
    page = client.get(f"/garages/{g1.id}").get_data(as_text=True)
    # Первый по номеру — предыдущего нет (по id им был бы гараж № 10).
    assert f"/garages/{g2.id}?" in page and f"/garages/{g10.id}?" not in page
