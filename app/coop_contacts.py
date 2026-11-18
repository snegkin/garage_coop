"""
Сайт и почта кооператива в документах (бланки, выписки, расчёты пени,
страницы об обработке ПД) — с учётом второго домена (алиаса).

У сайта может быть два адреса: основной (Cooperative.website, например
гм-восход.рф) и алиас латиницей (Cooperative.domain_alias, gm-voshod.ru).
Председатель выбирает, что показывать — основной, алиас или оба
(site_display / email_display: "main" | "alias" | "both"). Почта на алиасе —
тот же ящик с доменом алиаса: pravlenie@гм-восход.рф -> pravlenie@gm-voshod.ru
(латиницу проще набрать и не каждый почтовый сервис принимает кириллический
адрес).
"""
from urllib.parse import urlparse

DISPLAY_CHOICES = ("main", "alias", "both")


def host(website: str | None) -> str | None:
    """Адрес сайта без схемы, пути и завершающего слеша — для печати."""
    if not website:
        return None
    raw = website.strip()
    if "//" not in raw:
        raw = f"//{raw}"
    return urlparse(raw).netloc or None


def _alias(coop) -> str | None:
    return host(coop.domain_alias) if coop and coop.domain_alias else None


def _pick(main: str | None, alias: str | None, mode: str | None) -> list[str]:
    if mode == "alias" and alias:
        return [alias]
    if mode == "both" and alias and main and alias != main:
        return [main, alias]
    return [v for v in (main or alias,) if v]


def sites(coop) -> list[str]:
    if coop is None:
        return []
    return _pick(host(coop.website), _alias(coop), coop.site_display)


def emails(coop) -> list[str]:
    if coop is None or not coop.email:
        return []
    main = coop.email.strip()
    alias = _alias(coop)
    alias_email = f"{main.rsplit('@', 1)[0]}@{alias}" if alias and "@" in main else None
    return _pick(main, alias_email, coop.email_display)


def primary_email(coop) -> str | None:
    """Один адрес — для ссылки mailto: первый из показываемых."""
    found = emails(coop)
    return found[0] if found else None


def contacts_line(coop, with_phone: bool = True):
    """Строка контактов для шапки документа: «тел. … · почта · сайт».
    Каждый контакт — неразрывным куском (white-space: nowrap): перенос
    строки возможен только между контактами, а не посреди адреса
    («гм-» / «восход.рф» по дефису)."""
    from markupsafe import Markup, escape
    from .i18n import translate as _

    parts = []
    if with_phone and coop is not None and coop.phone:
        parts.append(f"{_('тел.')} {coop.phone}")
    parts += emails(coop) + sites(coop)
    return Markup(" · ").join(
        Markup('<span style="white-space: nowrap">{}</span>').format(escape(p)) for p in parts
    )
