"""
Навигация между страницами: ссылки «Назад» (data-back-link — адрес
возврата подставляет JS в base.html: initBackLinks), переходы
человек ↔ его гаражи, отсутствие вложенных <form> в профиле.
"""
import html.parser

from app.models import RoleEnum, GarageContact, News

from tests.conftest import make_person, make_garage, make_ownership, make_user, login


class _NestedFormFinder(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.depth = 0
        self.nested = 0

    def handle_starttag(self, tag, attrs):
        if tag == "form":
            if self.depth:
                self.nested += 1
            self.depth += 1

    def handle_endtag(self, tag):
        if tag == "form" and self.depth:
            self.depth -= 1


def test_person_detail_links_to_owned_and_contact_garages(db, client):
    person = make_person(db, full_name="Гаражов Гараж Гаражович")
    owned = make_garage(db, number="101")
    make_ownership(db, owned, person, share="0.5")
    contact = make_garage(db, number="102")
    db.add(GarageContact(garage_id=contact.id, person_id=person.id, relation="супруг"))
    make_user(db, "board_nav", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board_nav", "pass12345")

    body = client.get(f"/persons/{person.id}").get_data(as_text=True)
    assert f'href="/garages/{owned.id}"' in body
    assert f'href="/garages/{contact.id}"' in body
    assert "супруг" in body
    assert 'href="/persons/" class="d-inline-block mb-3" data-back-link' in body


def test_member_sees_own_garages_on_own_card_and_self_link_on_garage(db, client):
    person = make_person(db, full_name="Членов Член Членович")
    garage = make_garage(db, number="103")
    make_ownership(db, garage, person)
    other = make_person(db, full_name="Соседов Сосед Соседович")
    make_ownership(db, garage, other, share="0.5")
    make_user(db, "member_nav", "pass12345", role=RoleEnum.MEMBER, person=person)
    db.commit()
    login(client, "member_nav", "pass12345")

    body = client.get(f"/persons/{person.id}").get_data(as_text=True)
    assert f'href="/garages/{garage.id}"' in body

    body = client.get(f"/garages/{garage.id}").get_data(as_text=True)
    assert f'href="/persons/{person.id}"' in body         # на себя — ссылка
    assert f'href="/persons/{other.id}"' not in body      # на чужую карточку — нет (403)
    assert 'href="/cabinet/garages" class="d-inline-block mb-3" data-back-link' in body


def test_profile_has_no_nested_forms(db, client):
    person = make_person(db, full_name="Профилев Проф Профилевич")
    make_user(db, "member_prof", "pass12345", role=RoleEnum.MEMBER, person=person)
    db.commit()
    login(client, "member_prof", "pass12345")

    body = client.get("/cabinet/profile").get_data(as_text=True)
    finder = _NestedFormFinder()
    finder.feed(body)
    assert finder.nested == 0
    assert 'form="telegramLinkForm"' in body
    assert 'id="telegramLinkForm"' in body


def test_news_back_link_goes_to_home_not_login(db, client):
    item = News(title="Собрание", body="Текст")
    db.add(item)
    db.commit()

    body = client.get(f"/news/{item.id}").get_data(as_text=True)
    assert '<a href="/" class="d-inline-block mb-3" data-back-link' in body
    assert 'href="/auth/login" class="d-inline-block mb-3"' not in body
