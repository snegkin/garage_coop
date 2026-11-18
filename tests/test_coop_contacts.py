"""
Сайт и почта кооператива в документах (app/coop_contacts.py): без схемы,
каждый контакт неразрывно, второй домен (алиас) вместо основного или вместе.
"""
from types import SimpleNamespace

from app import coop_contacts


def _coop(**kw):
    base = dict(phone=None, email="pravlenie@гм-восход.рф", website="http://гм-восход.рф/",
                domain_alias=None, site_display="main", email_display="main")
    base.update(kw)
    return SimpleNamespace(**base)


def test_main_only_by_default_without_scheme():
    coop = _coop(domain_alias="gm-voshod.ru")
    assert coop_contacts.sites(coop) == ["гм-восход.рф"]
    assert coop_contacts.emails(coop) == ["pravlenie@гм-восход.рф"]


def test_alias_instead():
    coop = _coop(domain_alias="https://gm-voshod.ru", site_display="alias", email_display="alias")
    assert coop_contacts.sites(coop) == ["gm-voshod.ru"]
    assert coop_contacts.emails(coop) == ["pravlenie@gm-voshod.ru"]
    assert coop_contacts.primary_email(coop) == "pravlenie@gm-voshod.ru"


def test_both():
    coop = _coop(domain_alias="gm-voshod.ru", site_display="both", email_display="alias")
    assert coop_contacts.sites(coop) == ["гм-восход.рф", "gm-voshod.ru"]
    assert coop_contacts.emails(coop) == ["pravlenie@gm-voshod.ru"]


def test_alias_mode_without_alias_falls_back_to_main():
    coop = _coop(site_display="alias", email_display="both")
    assert coop_contacts.sites(coop) == ["гм-восход.рф"]
    assert coop_contacts.emails(coop) == ["pravlenie@гм-восход.рф"]


def test_contacts_line_is_nowrap_per_contact(app):
    with app.test_request_context():
        line = coop_contacts.contacts_line(_coop(phone="+7 900 000-00-00", domain_alias="gm-voshod.ru",
                                                 site_display="both"))
    assert line.count('white-space: nowrap') == 4
    assert "http://" not in line
    assert " · " in line
