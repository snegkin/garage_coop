/*
 * Оффлайн-страница правления (/offline/, app/templates/offline/app.html):
 * поиск гаража / человека / счётчика, карточки, должники, контрольные
 * счётчики, схема территории (static/offline/map.js) с карточками
 * оборудования, вики — по слепку из IndexedDB (static/offline/store.js).
 * Навигация — по #адресу, чтобы «Назад» на телефоне работал как обычно:
 * #g/<id> гараж, #p/<id> человек, #o/<id> оборудование, #w/<id> страница вики,
 * #map, #map/g/<id>, #map/o/<id> — схема с выделенным гаражом/объектом.
 */
(function () {
  "use strict";
  var view = document.getElementById("olView");
  var banner = document.getElementById("olBanner");
  var stamp = document.getElementById("olStamp");
  var refreshBtn = document.getElementById("olRefresh");
  var tabs = document.getElementById("olTabs");

  var rec = null;      // {etag, fetchedAt, data}
  var idx = null;      // индексы по слепку
  var query = "";      // строка поиска — сохраняется при переходах

  // ---------------------------------------------------------------- утилиты
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  var moneyFmt = new Intl.NumberFormat("ru-RU", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  function money(v) { return moneyFmt.format(Math.abs(v)) + " ₽"; }
  function cents(s) { return Math.round(parseFloat(s || "0") * 100); }
  function balanceHtml(c) {
    if (c < 0) return '<span class="ol-debt text-nowrap">долг ' + money(c / 100) + "</span>";
    if (c > 0) return '<span class="ol-over text-nowrap">переплата ' + money(c / 100) + "</span>";
    return '<span class="text-body-secondary">0 ₽</span>';
  }
  function date(iso) {
    if (!iso) return "";
    var p = iso.split("-");
    return p[2] + "." + p[1] + "." + p[0];
  }
  function stampText(ms) {
    var d = new Date(ms);
    var pad = function (n) { return (n < 10 ? "0" : "") + n; };
    return pad(d.getDate()) + "." + pad(d.getMonth() + 1) + " " + pad(d.getHours()) + ":" + pad(d.getMinutes());
  }
  function norm(s) { return String(s || "").toLowerCase().replace(/ё/g, "е").trim(); }
  function digits(s) { return String(s || "").replace(/\D/g, ""); }
  function telHref(n) { return "tel:" + String(n).replace(/[^\d+]/g, ""); }
  function byNumber(a, b) {
    var na = parseInt(a.number, 10), nb = parseInt(b.number, 10);
    if (!isNaN(na) && !isNaN(nb) && na !== nb) return na - nb;
    return String(a.number).localeCompare(String(b.number), "ru");
  }

  // --------------------------------------------------------------- индексы
  function buildIndex(data) {
    var i = { garage: {}, person: {}, feeType: {}, control: {}, personGarages: {}, accountsByPerson: {}, accountsByGarage: {},
      object: {}, onMap: {}, wiki: {}, wikiBySlug: {}, wikiChildren: {}, garageState: {} };
    data.fee_types.forEach(function (f) { i.feeType[f.id] = f; });
    data.persons.forEach(function (p) { i.person[p.id] = p; });
    data.garages.forEach(function (g) {
      i.garage[g.id] = g;
      g.owners.forEach(function (o) {
        (i.personGarages[o.person_id] = i.personGarages[o.person_id] || []).push({ garage: g, role: "owner" });
      });
      g.contacts.forEach(function (c) {
        (i.personGarages[c.person_id] = i.personGarages[c.person_id] || []).push({ garage: g, role: "contact", contact: c });
      });
    });
    data.accounts.forEach(function (a) {
      (i.accountsByPerson[a.person_id] = i.accountsByPerson[a.person_id] || []).push(a);
      if (a.garage_id != null) (i.accountsByGarage[a.garage_id] = i.accountsByGarage[a.garage_id] || []).push(a);
    });
    data.control_meters.forEach(function (c) { i.control[c.id] = c; });
    if (data.map) {
      data.map.objects.forEach(function (o) { i.object[o.id] = o; });
      data.map.shapes.forEach(function (s) { if (s.garage_id) i.onMap[s.garage_id] = true; });
    }
    (data.wiki || []).forEach(function (w) {
      i.wiki[w.id] = w;
      i.wikiBySlug[w.slug] = w;
      var k = w.parent_id == null ? "root" : w.parent_id;
      (i.wikiChildren[k] = i.wikiChildren[k] || []).push(w);
    });
    return i;
  }

  // Долг человека: взносы по его лицевым счетам + электричество гаражей, где
  // он собственник (как на сайте — весь долг гаража на каждого собственника).
  function personDebt(pid) {
    var parts = {}, total = 0;
    (idx.accountsByPerson[pid] || []).forEach(function (a) {
      var c = cents(a.balance);
      if (c >= 0) return;
      var key = "fee:" + a.fee_type_id;
      parts[key] = (parts[key] || 0) + c;
      total += c;
    });
    (idx.personGarages[pid] || []).forEach(function (pg) {
      if (pg.role !== "owner" || pg.garage.electricity_balance == null) return;
      var c = cents(pg.garage.electricity_balance);
      if (c >= 0) return;
      parts.electricity = (parts.electricity || 0) + c;
      total += c;
    });
    return { total: total, parts: parts };
  }
  // Окраска бокса на схеме — как garage_map._garage_states.
  var STALE_READING_DAYS = 62;
  function garageState(id) {
    if (id in idx.garageState) return idx.garageState[id];
    var g = idx.garage[id];
    if (!g) return (idx.garageState[id] = null);
    var total = g.electricity_balance == null ? 0 : cents(g.electricity_balance);
    (idx.accountsByGarage[id] || []).forEach(function (a) { total += cents(a.balance); });
    var power = "nometer";
    if (g.meter) {
      var last = g.meter.last_date ? new Date(g.meter.last_date + "T00:00:00") : null;
      power = last && (Date.now() - last.getTime()) / 86400000 <= STALE_READING_DAYS ? "fresh" : "stale";
    }
    var owners = g.owners.map(function (o) { var p = idx.person[o.person_id]; return p ? p.name : ""; }).filter(Boolean);
    return (idx.garageState[id] = {
      territory: g.outside ? "outside" : "inside", debt: total < 0 ? "debt" : "ok", power: power,
      land: g.land_privatized ? "privatized" : "notprivatized", owners: owners.join(", ")
    });
  }

  function partLabel(key) {
    if (key === "electricity") return "электричество";
    var f = idx.feeType[parseInt(key.slice(4), 10)];
    return f ? f.name : "взнос";
  }
  function garageNumbers(pid, role) {
    return (idx.personGarages[pid] || [])
      .filter(function (pg) { return !role || pg.role === role; })
      .map(function (pg) { return pg.garage; })
      .sort(byNumber)
      .map(function (g) { return "№" + g.number; });
  }

  // ----------------------------------------------------------- шапка, баннер
  var STANDALONE = (window.matchMedia && window.matchMedia("(display-mode: standalone)").matches) || window.navigator.standalone === true;
  var IOS = /iPad|iPhone|iPod/.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);

  function renderBanner(note) {
    var html = "";
    if (location.pathname.replace(/\/+$/, "") !== "/offline") {
      html += '<div class="alert alert-warning py-2">Сайт сейчас недоступен — показаны сохранённые данные. ' +
              '<a href="" class="alert-link" onclick="location.reload();return false;">Повторить</a></div>';
    }
    if (rec && !coopOffline.isExpired(rec)) {
      var ageDays = (Date.now() - rec.fetchedAt) / 86400000;
      if (ageDays > 7) {
        html += '<div class="alert alert-warning py-2">Данным больше недели. Обновите их, когда будет интернет.</div>';
      }
    }
    if (!STANDALONE) {
      html += IOS
        ? '<div class="alert alert-info py-2 small">Чтобы данные открывались без сети, добавьте сайт на экран «Домой» в Safari: ' +
          '<i class="bi bi-box-arrow-up"></i> «Поделиться» → «На экран „Домой“». Открывайте сайт с этого ярлыка: у него свои данные, отдельные от Safari.</div>'
        : '<div class="alert alert-info py-2 small">Чтобы данные открывались без сети, установите сайт как приложение: меню браузера → «Установить приложение» или «Добавить на главный экран».</div>';
    }
    if (note) html += note;
    banner.innerHTML = html;
  }

  function renderStamp() {
    stamp.textContent = rec ? "данные на " + stampText(rec.fetchedAt) : "нет данных";
  }

  function doRefresh() {
    refreshBtn.disabled = true;
    refreshBtn.innerHTML = '<span class="spinner-border spinner-border-sm" aria-hidden="true"></span>';
    coopOffline.refresh({ force: true }).then(function (result) {
      refreshBtn.disabled = false;
      refreshBtn.innerHTML = '<i class="bi bi-arrow-clockwise" aria-hidden="true"></i>';
      var note = "";
      if (result === "failed") note = '<div class="alert alert-secondary py-2">Сайт не отвечает — обновить не удалось. Показаны прежние данные.</div>';
      if (result === "denied") note = '<div class="alert alert-danger py-2">Нужно войти на сайт под учётной записью правления — данные на этом телефоне стёрты. <a class="alert-link" href="/auth/login?next=/offline/">Войти</a></div>';
      if (result === "login") note = '<div class="alert alert-warning py-2">Чтобы отправить записи и обновить данные, войдите на сайт. <a class="alert-link" href="/auth/login?next=/offline/">Войти</a></div>';
      return start(note);
    });
  }
  refreshBtn.addEventListener("click", doRefresh);

  // ----------------------------------------------------------------- поиск
  function search(q) {
    var nq = norm(q), dq = digits(q);
    if (!nq) return { garages: [], persons: [], objects: [], wiki: [] };
    var garages = [], persons = [], seenG = {}, seenP = {};
    var data = rec.data;
    data.garages.forEach(function (g) {
      var num = norm(g.number);
      if (num === nq || num.indexOf(nq) === 0 ||
          (g.meter && dq.length >= 3 && digits(g.meter.number).indexOf(dq) !== -1)) {
        garages.push(g); seenG[g.id] = true;
      }
    });
    garages.sort(function (a, b) {
      var ea = norm(a.number) === nq ? 0 : 1, eb = norm(b.number) === nq ? 0 : 1;
      return ea - eb || byNumber(a, b);
    });
    data.persons.forEach(function (p) {
      var hit = norm(p.name).indexOf(nq) !== -1;
      if (!hit && dq.length >= 3) {
        hit = p.phones.some(function (ph) { return digits(ph.number).indexOf(dq) !== -1; });
      }
      if (hit) { persons.push(p); seenP[p.id] = true; }
    });
    var objects = data.map ? data.map.objects.filter(function (o) {
      return norm(o.title).indexOf(nq) !== -1 || norm(offlineMap.KINDS[o.kind]).indexOf(nq) === 0 ||
        (o.ip_address && o.ip_address.indexOf(q.trim()) === 0);
    }) : [];
    var wiki = (data.wiki || []).filter(function (w) { return norm(w.title).indexOf(nq) !== -1; });
    return { garages: garages.slice(0, 50), persons: persons.slice(0, 50), objects: objects.slice(0, 30), wiki: wiki.slice(0, 30) };
  }

  function garageRow(g) {
    var owners = g.owners.map(function (o) { var p = idx.person[o.person_id]; return p ? p.name : ""; }).filter(Boolean);
    var bal = g.electricity_balance == null ? "" : balanceHtml(cents(g.electricity_balance));
    return '<a class="list-group-item list-group-item-action" href="#g/' + g.id + '">' +
      '<div class="d-flex justify-content-between gap-2"><strong>Гараж №' + esc(g.number) + "</strong><span>" + bal + "</span></div>" +
      '<div class="small text-body-secondary">' + (owners.length ? esc(owners.join(", ")) : "собственник не указан") +
      (g.meter ? " · счётчик " + esc(g.meter.number) : "") + "</div></a>";
  }

  function personRow(p) {
    var d = personDebt(p.id);
    var nums = garageNumbers(p.id);
    return '<a class="list-group-item list-group-item-action" href="#p/' + p.id + '">' +
      '<div class="d-flex justify-content-between gap-2"><span>' + esc(p.name) +
      (p.archived ? ' <span class="badge text-bg-secondary">в архиве</span>' : "") + "</span>" +
      (d.total < 0 ? balanceHtml(d.total) : "") + "</div>" +
      (nums.length ? '<div class="small text-body-secondary">' + esc(nums.join(", ")) + "</div>" : "") + "</a>";
  }

  function renderSearch() {
    view.innerHTML =
      '<input id="olQ" type="search" class="form-control ol-search mb-3" inputmode="search" autocomplete="off" ' +
      'placeholder="Номер гаража, фамилия, телефон, номер счётчика" value="' + esc(query) + '">' +
      '<div id="olResults"></div>' +
      '<a class="btn btn-outline-primary btn-sm mt-2" href="#f/person"><i class="bi bi-person-plus" aria-hidden="true"></i> Новый человек</a>';
    var input = document.getElementById("olQ");
    var results = document.getElementById("olResults");
    function update() {
      query = input.value;
      var r = search(query);
      if (!norm(query)) {
        results.innerHTML = '<p class="text-body-secondary">Гаражей: ' + rec.data.garages.length +
          ", людей: " + rec.data.persons.length + ".</p>";
        return;
      }
      if (!r.garages.length && !r.persons.length && !r.objects.length && !r.wiki.length) {
        results.innerHTML = '<p class="text-body-secondary">Ничего не найдено.</p>';
        return;
      }
      var html = "";
      if (r.garages.length) html += '<div class="list-group ol-list mb-3">' + r.garages.map(garageRow).join("") + "</div>";
      if (r.persons.length) html += '<div class="list-group ol-list mb-3">' + r.persons.map(personRow).join("") + "</div>";
      if (r.objects.length) html += '<div class="list-group ol-list mb-3">' + r.objects.map(objectRow).join("") + "</div>";
      if (r.wiki.length) html += '<div class="list-group ol-list mb-3">' + r.wiki.map(function (w) {
        return '<a class="list-group-item list-group-item-action" href="#w/' + w.id + '"><i class="bi bi-book me-1" aria-hidden="true"></i>' + esc(w.title) + "</a>";
      }).join("") + "</div>";
      results.innerHTML = html;
    }
    input.addEventListener("input", update);
    update();
    if (!query) input.focus();
  }

  // -------------------------------------------------------------- карточки
  function contactsHtml(p) {
    var items = [];
    p.phones.forEach(function (ph) {
      items.push('<a class="btn btn-sm btn-outline-primary ol-tel me-1 mb-1" href="' + esc(telHref(ph.number)) + '">' +
        '<i class="bi bi-telephone" aria-hidden="true"></i> ' + esc(ph.number) + "</a>" +
        (ph.label ? '<small class="text-body-secondary me-2">' + esc(ph.label) + "</small>" : ""));
    });
    var extra = [];
    if (p.email) extra.push('<i class="bi bi-envelope" aria-hidden="true"></i> ' + esc(p.email));
    if (p.telegram) extra.push('<i class="bi bi-telegram" aria-hidden="true"></i> ' + esc(p.telegram));
    if (p.max) extra.push("MAX " + esc(p.max));
    return (items.length ? '<div class="mt-1">' + items.join("") + "</div>" : '<div class="small text-body-secondary">телефон не указан</div>') +
      (extra.length ? '<div class="small text-body-secondary mt-1">' + extra.join(" · ") + "</div>" : "");
  }

  function accountsTable(accounts, showPerson) {
    if (!accounts.length) return "";
    return '<ul class="list-group list-group-flush">' + accounts.map(function (a) {
      var f = idx.feeType[a.fee_type_id];
      var who = showPerson && idx.person[a.person_id] ? '<div class="small text-body-secondary">' + esc(idx.person[a.person_id].name) + "</div>" : "";
      var where = !showPerson && a.garage_id != null && idx.garage[a.garage_id] ? " · №" + esc(idx.garage[a.garage_id].number) : "";
      return '<li class="list-group-item d-flex justify-content-between gap-2"><div>' + esc(f ? f.name : "взнос") + where +
        '<div class="small text-body-secondary">счёт ' + esc(a.number) + "</div>" + who + "</div><div>" + balanceHtml(cents(a.balance)) + "</div></li>";
    }).join("") + "</ul>";
  }

  function renderGarage(id) {
    var g = idx.garage[id];
    if (!g) { view.innerHTML = '<p class="text-body-secondary">Гараж не найден.</p>'; return; }
    var html = '<h4 class="mb-3">Гараж №' + esc(g.number) +
      (g.outside ? ' <span class="badge text-bg-secondary align-middle">за территорией</span>' : "") + "</h4>";
    html += garageActions(g);

    html += '<div class="card mb-3"><div class="card-header">Собственники</div><ul class="list-group list-group-flush">';
    if (!g.owners.length) html += '<li class="list-group-item text-body-secondary">не указаны</li>';
    g.owners.forEach(function (o) {
      var p = idx.person[o.person_id];
      if (!p) return;
      var share = parseFloat(o.share) < 1 ? ' <small class="text-body-secondary">доля ' + esc(o.share) + "</small>" : "";
      html += '<li class="list-group-item"><a href="#p/' + p.id + '">' + esc(p.name) + "</a>" + share + contactsHtml(p) + "</li>";
    });
    html += "</ul></div>";

    if (g.contacts.length) {
      html += '<div class="card mb-3"><div class="card-header">Для связи</div><ul class="list-group list-group-flush">';
      g.contacts.forEach(function (c) {
        var p = idx.person[c.person_id];
        if (!p) return;
        var rel = [c.tenant ? "арендатор" : "", c.relation || ""].filter(Boolean).join(", ");
        html += '<li class="list-group-item"><a href="#p/' + p.id + '">' + esc(p.name) + "</a>" +
          (rel ? ' <small class="text-body-secondary">' + esc(rel) + "</small>" : "") + contactsHtml(p) + "</li>";
      });
      html += "</ul></div>";
    }

    html += '<div class="card mb-3"><div class="card-header">Электричество</div><ul class="list-group list-group-flush">';
    html += '<li class="list-group-item d-flex justify-content-between"><span>Баланс</span>' +
      (g.electricity_balance == null ? '<span class="text-body-secondary">нет начислений</span>' : balanceHtml(cents(g.electricity_balance))) + "</li>";
    if (g.meter) {
      html += '<li class="list-group-item d-flex justify-content-between"><span>Счётчик</span><span>' + esc(g.meter.number) + "</span></li>";
      html += '<li class="list-group-item d-flex justify-content-between"><span>Последнее показание</span><span>' +
        (g.meter.last_reading != null ? "<strong>" + esc(g.meter.last_reading) + "</strong> от " + date(g.meter.last_date) : '<span class="text-body-secondary">нет</span>') +
        "</span></li>";
    } else {
      html += '<li class="list-group-item text-body-secondary">Счётчик не указан</li>';
    }
    html += "</ul></div>";

    var accounts = idx.accountsByGarage[g.id] || [];
    if (accounts.length) {
      html += '<div class="card mb-3"><div class="card-header">Взносы по гаражу</div>' + accountsTable(accounts, true) + "</div>";
    }
    if (g.comment) html += '<div class="card mb-3"><div class="card-body small" style="white-space:pre-line">' + esc(g.comment) + "</div></div>";
    if (idx.onMap[g.id]) html += '<a class="btn btn-outline-primary btn-sm me-2" href="#map/g/' + g.id + '"><i class="bi bi-map" aria-hidden="true"></i> На схеме</a>';
    html += '<a class="btn btn-outline-secondary btn-sm" href="/garages/' + g.id + '">Открыть на сайте</a>';
    view.innerHTML = html;
  }

  function renderPerson(id) {
    var p = idx.person[id];
    if (!p) { view.innerHTML = '<p class="text-body-secondary">Человек не найден.</p>'; return; }
    var d = personDebt(p.id);
    var html = '<h4 class="mb-1">' + esc(p.name) + "</h4>" +
      (p.archived ? '<span class="badge text-bg-secondary mb-2">в архиве</span>' : "") +
      '<div class="mb-3">' + contactsHtml(p) + "</div>" + personActions(p);

    html += '<div class="card mb-3"><div class="card-header d-flex justify-content-between"><span>Долг</span>' +
      (d.total < 0 ? balanceHtml(d.total) : '<span class="text-body-secondary">нет</span>') + "</div>";
    var keys = Object.keys(d.parts);
    if (keys.length) {
      html += '<ul class="list-group list-group-flush">' + keys.map(function (k) {
        return '<li class="list-group-item d-flex justify-content-between"><span>' + esc(partLabel(k)) + "</span>" + balanceHtml(d.parts[k]) + "</li>";
      }).join("") + "</ul>";
    }
    html += "</div>";

    var pgs = (idx.personGarages[p.id] || []).slice().sort(function (a, b) { return byNumber(a.garage, b.garage); });
    if (pgs.length) {
      html += '<div class="list-group ol-list mb-3">' + pgs.map(function (pg) {
        var g = pg.garage;
        var role = pg.role === "owner" ? "собственник" : (pg.contact.tenant ? "арендатор" : (pg.contact.relation || "для связи"));
        return '<a class="list-group-item list-group-item-action d-flex justify-content-between gap-2" href="#g/' + g.id + '">' +
          "<span>Гараж №" + esc(g.number) + ' <small class="text-body-secondary">' + esc(role) + "</small></span>" +
          (g.electricity_balance != null ? "<span>" + balanceHtml(cents(g.electricity_balance)) + "</span>" : "") + "</a>";
      }).join("") + "</div>";
    }

    var accounts = idx.accountsByPerson[p.id] || [];
    if (accounts.length) html += '<div class="card mb-3"><div class="card-header">Лицевые счета</div>' + accountsTable(accounts, false) + "</div>";
    if (p.comment) html += '<div class="card mb-3"><div class="card-body small" style="white-space:pre-line">' + esc(p.comment) + "</div></div>";
    html += '<a class="btn btn-outline-secondary btn-sm" href="/persons/' + p.id + '">Открыть на сайте</a>';
    view.innerHTML = html;
  }

  // -------------------------------------------------------------- должники
  var debtorFilter = "all";
  function renderDebtors() {
    var rows = [];
    rec.data.persons.forEach(function (p) {
      var d = personDebt(p.id);
      var amount = debtorFilter === "all" ? d.total : (d.parts[debtorFilter] || 0);
      if (amount < 0) rows.push({ p: p, amount: amount, d: d });
    });
    rows.sort(function (a, b) { return a.amount - b.amount; });
    var total = rows.reduce(function (s, r) { return s + r.amount; }, 0);

    var options = [["all", "Все долги"], ["electricity", "Электричество"]].concat(
      rec.data.fee_types.map(function (f) { return ["fee:" + f.id, f.name]; }));
    var html = '<select id="olDebtFilter" class="form-select mb-2">' + options.map(function (o) {
      return '<option value="' + esc(o[0]) + '"' + (o[0] === debtorFilter ? " selected" : "") + ">" + esc(o[1]) + "</option>";
    }).join("") + "</select>";
    html += '<p class="small text-body-secondary mb-2">Должников: ' + rows.length + (rows.length ? ", всего " + money(total / 100) : "") +
      ". Электричество гаража с несколькими собственниками считается за каждым.</p>";
    html += '<div class="list-group ol-list">' + rows.map(function (r) {
      var parts = Object.keys(r.d.parts).map(function (k) { return partLabel(k) + " " + money(r.d.parts[k] / 100); });
      var nums = garageNumbers(r.p.id, "owner");
      return '<a class="list-group-item list-group-item-action" href="#p/' + r.p.id + '">' +
        '<div class="d-flex justify-content-between gap-2"><span>' + esc(r.p.name) + "</span>" + balanceHtml(r.amount) + "</div>" +
        '<div class="small text-body-secondary">' + esc(nums.join(", ")) + (debtorFilter === "all" && parts.length > 1 ? " · " + esc(parts.join(", ")) : "") + "</div></a>";
    }).join("") + "</div>";
    view.innerHTML = html;
    document.getElementById("olDebtFilter").addEventListener("change", function () {
      debtorFilter = this.value;
      renderDebtors();
    });
  }

  // --------------------------------------------------------- фото на весь экран
  function openLightbox(src) {
    var box = document.createElement("div");
    box.id = "olLightbox";
    box.innerHTML = '<img alt="" src="' + esc(src) + '">';
    box.addEventListener("click", function () { box.remove(); });
    document.body.appendChild(box);
  }
  view.addEventListener("click", function (evt) {
    var t = evt.target;
    if (t.tagName === "IMG" && (t.closest(".ol-thumbs") || t.closest(".ol-wiki"))) {
      evt.preventDefault();
      openLightbox(t.getAttribute("src"));
      return;
    }
    if (t.classList && t.classList.contains("wiki-spoiler")) { t.classList.toggle("revealed"); return; }
    // Ссылки вики на другие страницы вики — на их оффлайн-копию.
    var a = t.closest && t.closest(".ol-wiki a[href]");
    if (a) {
      var url = new URL(a.getAttribute("href"), location.origin);
      var m = url.origin === location.origin && /^\/wiki\/([^\/]+)\/?$/.exec(url.pathname);
      if (m) {
        var key = decodeURIComponent(m[1]);
        var page = idx.wikiBySlug[key] || (/^\d+$/.test(key) ? idx.wiki[parseInt(key, 10)] : null);
        if (page) { evt.preventDefault(); location.hash = "w/" + page.id; }
      }
    }
  });
  function photosHtml(photos) {
    if (!photos.length) return "";
    return '<div class="d-flex flex-wrap gap-2 ol-thumbs mb-3">' + photos.map(function (ph) {
      return '<figure class="mb-0"><img loading="lazy" alt="" src="' + esc(ph.url) + '">' +
        (ph.caption ? '<figcaption class="small text-body-secondary">' + esc(ph.caption) + "</figcaption>" : "") + "</figure>";
    }).join("") + "</div>";
  }

  // ----------------------------------------------------------------- схема
  function renderMap(focus) {
    if (!rec.data.map) { view.innerHTML = '<p class="text-body-secondary">Схема территории не заведена.</p>'; return; }
    offlineMap.render(view, rec.data.map, {
      state: garageState,
      number: function (id) { return idx.garage[id] ? idx.garage[id].number : ""; },
      focus: focus
    });
  }

  var NET_ROLES = { router: "роутер", hotspot: "точка доступа", bridge: "мост", repeater: "повторитель" };
  function objectRow(o) {
    return '<a class="list-group-item list-group-item-action" href="#o/' + o.id + '">' +
      '<span class="text-body-secondary">' + esc(offlineMap.KINDS[o.kind] || "") + "</span> " + esc(o.title || "") +
      (o.ip_address ? ' <small class="text-body-secondary">' + esc(o.ip_address) + "</small>" : "") + "</a>";
  }
  function row(label, value) {
    return '<li class="list-group-item d-flex justify-content-between gap-2"><span>' + label + '</span><span class="text-end">' + value + "</span></li>";
  }
  function renderObject(id) {
    var o = idx.object[id];
    if (!o) { view.innerHTML = '<p class="text-body-secondary">Объект не найден.</p>'; return; }
    var html = '<div class="text-body-secondary small">' + esc(offlineMap.KINDS[o.kind] || "") + "</div>" +
      '<h4 class="mb-3">' + esc(o.title || offlineMap.KINDS[o.kind] || "Объект") + "</h4>";
    if (o.description) html += '<div class="card mb-3"><div class="card-body" style="white-space:pre-line">' + esc(o.description) + "</div></div>";
    html += photosHtml(o.photos);
    var rows = [];
    if (o.master) {
      var mm = rec.data.map.master_meter;
      rows.push(row("Главный счётчик кооператива", mm ? "<strong>" + esc(mm.reading) + "</strong> от " + date(mm.date) : "нет показаний"));
    } else if (o.control_meter_id && idx.control[o.control_meter_id]) {
      var c = idx.control[o.control_meter_id];
      rows.push(row('<a href="#control">' + esc(c.name) + "</a>",
        c.last_reading != null ? "<strong>" + esc(c.last_reading) + "</strong> от " + date(c.last_date) : "нет показаний"));
    }
    if (o.model) rows.push(row("Модель", esc(o.model)));
    if (o.net_role) rows.push(row("Роль", esc(NET_ROLES[o.net_role] || o.net_role)));
    if (o.hostname) rows.push(row("Имя", esc(o.hostname)));
    if (o.ip_address) rows.push(row("IP", esc(o.ip_address)));
    if (o.mac) rows.push(row("MAC", esc(o.mac)));
    var up = o.uplink_id && idx.object[o.uplink_id];
    if (up) rows.push(row("Подключено к", '<a href="#o/' + up.id + '">' + esc(up.title || offlineMap.KINDS[up.kind]) + "</a>"));
    var down = rec.data.map.objects.filter(function (x) { return x.uplink_id === o.id; });
    if (down.length) rows.push(row("К нему подключены", down.map(function (x) {
      return '<a href="#o/' + x.id + '">' + esc(x.title || offlineMap.KINDS[x.kind]) + "</a>";
    }).join("<br>")));
    if (o.wiki_page_id && idx.wiki[o.wiki_page_id]) {
      rows.push(row("Вики", '<a href="#w/' + o.wiki_page_id + '">' + esc(idx.wiki[o.wiki_page_id].title) + "</a>"));
    }
    if (rows.length) html += '<ul class="list-group mb-3">' + rows.join("") + "</ul>";
    html += '<a class="btn btn-outline-primary btn-sm" href="#map/o/' + o.id + '"><i class="bi bi-map" aria-hidden="true"></i> На схеме</a>';
    view.innerHTML = html;
  }

  // ------------------------------------------------------------------ вики
  function renderWikiList() {
    var pages = rec.data.wiki || [];
    if (!pages.length) { view.innerHTML = '<p class="text-body-secondary">Страниц вики нет.</p>'; return; }
    var html = '<div class="list-group ol-list">';
    function walk(key, depth) {
      (idx.wikiChildren[key] || []).forEach(function (w) {
        html += '<a class="list-group-item list-group-item-action ol-tree-' + Math.min(depth, 3) + '" href="#w/' + w.id + '">' + esc(w.title) +
          (w.internal ? ' <span class="badge text-bg-secondary">внутренняя</span>' : "") + "</a>";
        walk(w.id, depth + 1);
      });
    }
    walk("root", 0);
    // Страницы, чей родитель не попал в слепок, — в корень.
    pages.forEach(function (w) {
      if (w.parent_id != null && !idx.wiki[w.parent_id]) {
        html += '<a class="list-group-item list-group-item-action" href="#w/' + w.id + '">' + esc(w.title) + "</a>";
      }
    });
    view.innerHTML = html + "</div>";
  }
  function renderWikiPage(id) {
    var w = idx.wiki[id];
    if (!w) { view.innerHTML = '<p class="text-body-secondary">Страница не найдена.</p>'; return; }
    var crumbs = [], cur = w.parent_id != null ? idx.wiki[w.parent_id] : null;
    while (cur) { crumbs.unshift(cur); cur = cur.parent_id != null ? idx.wiki[cur.parent_id] : null; }
    var html = '<nav class="small mb-2"><a href="#wiki">Вики</a>' + crumbs.map(function (c) {
      return ' / <a href="#w/' + c.id + '">' + esc(c.title) + "</a>";
    }).join("") + "</nav>";
    html += '<div class="card"><div class="card-body">' +
      '<h4 class="card-title">' + esc(w.title) + (w.internal ? ' <span class="badge text-bg-secondary align-middle">внутренняя</span>' : "") + "</h4>" +
      (w.updated ? '<div class="small text-body-secondary mb-3">обновлено ' + date(w.updated) + "</div>" : "") +
      '<div class="ol-wiki">' + w.html + "</div>";
    if (w.images.length) html += '<div class="mt-3">' + photosHtml(w.images.map(function (i) { return { url: i.url }; })) + "</div>";
    if (w.files.length) {
      html += '<div class="mt-3"><h6>Файлы</h6>' + w.files.map(function (f) {
        return '<div class="mb-2"><a class="btn btn-sm btn-outline-secondary" href="' + esc(f.url) + '">' + esc(f.name) + "</a>" +
          (f.offline ? "" : ' <small class="text-body-secondary">только при интернете</small>') + "</div>";
      }).join("") + "</div>";
    }
    var sub = idx.wikiChildren[w.id] || [];
    if (sub.length) html += '<div class="mt-3"><h6>Подразделы</h6><div class="list-group">' + sub.map(function (c) {
      return '<a class="list-group-item list-group-item-action" href="#w/' + c.id + '">' + esc(c.title) + "</a>";
    }).join("") + "</div></div>";
    view.innerHTML = html + "</div></div>";
  }

  // ======================================================= записи без сети
  // Очередь (static/offline/store.js) — что внесли на телефоне и ещё не
  // отправили или отправили недавно (с итогом от сайта).
  var queue = [];
  var KIND_NAMES = {
    reading: "Показания", control_reading: "Контрольные показания", meter: "Новый счётчик", payment: "Оплата",
    person_create: "Новый человек", person_edit: "Правка карточки", link: "Привязка к гаражу"
  };
  var RESULT_NAMES = {
    applied: ["внесено", "success"], pending: ["ждёт подтверждения председателя", "warning"],
    review: ["требует разбора на сайте", "danger"], rejected: ["отклонено", "secondary"], done: ["сделано на сайте", "info"]
  };
  var queueBtn = document.getElementById("olQueueBtn");
  var queueCount = document.getElementById("olQueueCount");

  function today() {
    var d = new Date(), pad = function (n) { return (n < 10 ? "0" : "") + n; };
    return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate());
  }
  function num(s) { var v = parseFloat(String(s || "").replace(",", ".").replace(/\s/g, "")); return isNaN(v) ? null : v; }
  function localEntries() { return queue.filter(function (e) { return e.state === "local"; }); }
  function entryGarageId(e) {
    var p = e.payload || {};
    return p.garage_id || (p.link && p.link.garage_id) || null;
  }
  function loadQueue() {
    return coopOffline.queueList().then(function (list) {
      queue = list;
      var n = localEntries().length;
      queueCount.textContent = n;
      queueBtn.classList.toggle("d-none", !queue.length);
      queueBtn.classList.toggle("btn-warning", n > 0);
      queueBtn.classList.toggle("btn-light", n === 0);
    });
  }
  function personName(id) { return idx.person[id] ? idx.person[id].name : "?"; }
  function garageNo(id) { return idx.garage[id] ? "№" + idx.garage[id].number : "?"; }
  function accountLabel(a) {
    var f = idx.feeType[a.fee_type_id];
    return (f ? f.name : "Взнос") + " · " + personName(a.person_id) + " · счёт " + a.number;
  }
  function accountById(id) {
    for (var i = 0; i < rec.data.accounts.length; i++) if (rec.data.accounts[i].id === id) return rec.data.accounts[i];
    return null;
  }

  function describeEntry(e) {
    var p = e.payload || {};
    switch (e.kind) {
      case "reading": return "Гараж " + garageNo(p.garage_id) + ", счётчик " + p.meter_number + ": " + p.value + " на " + date(p.date);
      case "control_reading": return "«" + (idx.control[p.control_meter_id] || { name: "?" }).name + "»: " + p.value + " на " + date(p.date);
      case "meter": return "Гараж " + garageNo(p.garage_id) + ": счётчик " + p.meter_number + (p.initial_reading ? ", начальные " + p.initial_reading : "");
      case "payment":
        var what = p.target === "electricity" ? "электричество, гараж " + garageNo(p.garage_id)
          : (accountById(p.account_id) ? accountLabel(accountById(p.account_id)) : "взнос");
        return money(num(p.amount)) + " — " + what + ", квитанция №" + p.receipt;
      case "person_create": return (p.fields || {}).full_name + (p.link && p.link.garage_id ? " — гараж " + garageNo(p.link.garage_id) : "");
      case "person_edit": return personName(p.person_id) + ": " + Object.keys(p.changes || {}).map(function (k) { return FIELD_NAMES[k] || k; }).join(", ");
      case "link": return personName(p.person_id) + " — гараж " + garageNo((p.link || {}).garage_id);
    }
    return "";
  }

  function entryHtml(e, withActions) {
    var res = e.result && RESULT_NAMES[e.result.status];
    var badge = e.state === "local" ? '<span class="badge text-bg-warning">не отправлено</span>'
      : '<span class="badge text-bg-' + (res ? res[1] : "secondary") + '">' + (res ? res[0] : e.result && e.result.status) + "</span>";
    var html = '<div class="list-group-item"><div class="d-flex justify-content-between gap-2"><strong>' + esc(KIND_NAMES[e.kind] || e.kind) + "</strong>" + badge + "</div>" +
      "<div>" + esc(describeEntry(e)) + "</div>" +
      '<div class="small text-body-secondary">записано ' + date(e.captured_at.slice(0, 10)) + " " + esc(e.captured_at.slice(11, 16)) + "</div>";
    if (e.result && e.result.message && e.result.status !== "applied") html += '<div class="small mt-1">' + esc(e.result.message) + "</div>";
    if (withActions) {
      html += '<div class="mt-2 d-flex gap-2">';
      if (e.kind === "payment") html += '<a class="btn btn-sm btn-outline-secondary" href="#receipt/' + e.uuid + '">Квитанция</a>';
      if (e.state === "local") html += '<button type="button" class="btn btn-sm btn-outline-danger" data-remove="' + e.uuid + '">Удалить</button>';
      html += "</div>";
    }
    return html + "</div>";
  }

  function queueSection(filter, title) {
    var list = queue.filter(filter).filter(function (e) { return e.state === "local" || (e.result && e.result.status !== "applied"); });
    if (!list.length) return "";
    return '<div class="card mb-3 border-warning"><div class="card-header">' + title + '</div><div class="list-group list-group-flush">' +
      list.map(function (e) { return entryHtml(e, true); }).join("") + "</div></div>";
  }

  function renderQueue() {
    var local = localEntries();
    var html = '<h4 class="mb-2">Записи без сети</h4>' +
      '<p class="small text-body-secondary">Отправляются сами, как только сайт станет доступен (при открытии сайта или этой страницы с интернетом). ' +
      "Пока запись не отправлена, её можно удалить, если ошиблись.</p>";
    if (local.length) html += '<button type="button" class="btn btn-primary mb-3" id="olSendNow">Отправить сейчас (' + local.length + ")</button>";
    if (!queue.length) html += '<p class="text-body-secondary">Записей нет.</p>';
    html += '<div class="list-group mb-3">' + queue.slice().reverse().map(function (e) { return entryHtml(e, true); }).join("") + "</div>";
    view.innerHTML = html;
    var btn = document.getElementById("olSendNow");
    if (btn) btn.addEventListener("click", doRefresh);
  }

  view.addEventListener("click", function (evt) {
    var t = evt.target.closest && evt.target.closest("[data-remove]");
    if (!t) return;
    if (!window.confirm("Удалить эту запись? Она не была отправлена на сайт.")) return;
    coopOffline.queueRemove(t.getAttribute("data-remove")).then(loadQueue).then(route);
  });

  // Сохранить запись и вернуться туда, откуда пришли (или на экран квитанции).
  function saveEntry(kind, payload, photo, next) {
    payload.captured_by = rec.data.user.username;
    return coopOffline.queueAdd(kind, payload, photo).then(function (entry) {
      return loadQueue().then(function () {
        location.hash = typeof next === "function" ? next(entry) : next;
        if (navigator.onLine !== false) {
          coopOffline.sendQueue().then(function (r) { if (r.sent) loadQueue().then(route); });
        }
      });
    });
  }

  // Фото с телефона — уменьшить до 1600 px по длинной стороне (JPEG), чтобы
  // очередь не разрасталась и отправка шла быстро.
  function compressPhoto(file) {
    return new Promise(function (resolve) {
      if (!file) { resolve(null); return; }
      var img = new Image();
      var url = URL.createObjectURL(file);
      img.onload = function () {
        var scale = Math.min(1, 1600 / Math.max(img.naturalWidth, img.naturalHeight));
        var canvas = document.createElement("canvas");
        canvas.width = Math.round(img.naturalWidth * scale);
        canvas.height = Math.round(img.naturalHeight * scale);
        canvas.getContext("2d").drawImage(img, 0, 0, canvas.width, canvas.height);
        URL.revokeObjectURL(url);
        canvas.toBlob(function (blob) { resolve(blob || file); }, "image/jpeg", 0.85);
      };
      img.onerror = function () { URL.revokeObjectURL(url); resolve(file); };
      img.src = url;
    });
  }

  function formShell(title, body, back) {
    return '<a class="small" href="' + back + '">← назад</a><h4 class="mt-2 mb-3">' + title + "</h4>" +
      '<form id="olForm" novalidate>' + body +
      '<div class="alert alert-danger py-2 d-none" id="olFormError"></div>' +
      '<button type="submit" class="btn btn-primary btn-lg w-100 mb-3">Сохранить</button></form>';
  }
  function field(label, input, hint) {
    return '<div class="mb-3"><label class="form-label">' + label + "</label>" + input +
      (hint ? '<div class="form-text">' + hint + "</div>" : "") + "</div>";
  }
  function input(name, attrs) {
    return '<input class="form-control" name="' + name + '" ' + (attrs || "") + ">";
  }
  function formError(text) {
    var box = document.getElementById("olFormError");
    box.textContent = text;
    box.classList.toggle("d-none", !text);
    if (text) box.scrollIntoView({ block: "center" });
  }
  function onSubmit(handler) {
    var form = document.getElementById("olForm");
    form.addEventListener("submit", function (evt) {
      evt.preventDefault();
      formError("");
      var btn = form.querySelector("[type=submit]");
      btn.disabled = true;
      new Promise(function (resolve) { resolve(handler(form)); }).catch(function (err) {
        formError(err && err.message ? err.message : "Не удалось сохранить.");
      }).finally(function () { btn.disabled = false; });
    });
    return form;
  }
  function fail(text) { throw new Error(text); }

  // ----- показания счётчика гаража
  // Текущий счётчик с учётом неотправленного «нового счётчика» и последнее
  // показание с учётом неотправленных показаний.
  function meterState(g) {
    var meter = g.meter ? { number: g.meter.number, last: num(g.meter.last_reading), date: g.meter.last_date } : null;
    localEntries().forEach(function (e) {
      if (e.payload.garage_id !== g.id) return;
      if (e.kind === "meter") meter = { number: e.payload.meter_number, last: num(e.payload.initial_reading), date: e.payload.installed_date, local: true };
      if (e.kind === "reading" && meter && e.payload.meter_number === meter.number) {
        meter.last = num(e.payload.value); meter.date = e.payload.date; meter.local = true;
      }
    });
    return meter;
  }

  var BIG_USAGE_KWH = 2000;  // больше — переспросить: скорее всего лишняя цифра

  function renderReadingForm(gid) {
    var g = idx.garage[gid];
    if (!g) return renderGarage(gid);
    var m = meterState(g);
    if (!m) {
      view.innerHTML = formShell("Показания, гараж №" + esc(g.number),
        '<p>У гаража не указан счётчик. <a href="#f/meter/' + g.id + '">Внести счётчик</a></p>', "#g/" + g.id).replace(/<button type="submit"[^<]*<\/button>/, "");
      return;
    }
    view.innerHTML = formShell("Показания, гараж №" + esc(g.number),
      '<p class="mb-2">Счётчик <strong>' + esc(m.number) + "</strong>" + (m.local ? ' <span class="badge text-bg-warning">не отправлено</span>' : "") + "<br>" +
      (m.last != null ? "Последнее: <strong>" + m.last + "</strong>" + (m.date ? " от " + date(m.date) : "") : "Показаний ещё нет") + "</p>" +
      field("Показания", input("value", 'inputmode="decimal" autocomplete="off" required style="font-size:1.4rem"'), '<span id="olDelta"></span>') +
      field("Дата снятия", input("date", 'type="date" value="' + today() + '" max="' + today() + '"')) +
      field("Фото счётчика", '<input class="form-control" type="file" name="photo" accept="image/*" capture="environment">', "Необязательно — сохранится в журнале показаний.") +
      field("Комментарий", input("comment", 'autocomplete="off"')),
      "#g/" + g.id);
    var form = onSubmit(function (f) {
      var value = num(f.value.value);
      if (value == null) fail("Укажите показания.");
      if (m.last != null && value <= m.last) fail("Показания должны быть больше последних (" + m.last + "). Если счётчик заменили — сначала внесите новый счётчик.");
      if (f.date.value > today()) fail("Дата снятия не может быть в будущем.");
      if (m.date && f.date.value < m.date) fail("Дата снятия раньше последнего показания (" + date(m.date) + ").");
      var used = m.last != null ? value - m.last : 0;
      if (used > BIG_USAGE_KWH && !window.confirm("Расход " + Math.round(used) + " кВт·ч — это очень много. Проверьте цифры. Сохранить всё равно?")) return;
      return compressPhoto(f.photo.files[0]).then(function (photo) {
        return saveEntry("reading", { garage_id: g.id, meter_number: m.number, value: String(value), date: f.date.value,
          comment: f.comment.value.trim() }, photo, "#g/" + g.id);
      });
    });
    form.value.addEventListener("input", function () {
      var v = num(form.value.value);
      document.getElementById("olDelta").textContent = v != null && m.last != null ? "Расход: " + Math.round((v - m.last) * 100) / 100 + " кВт·ч" : "";
    });
    form.value.focus();
  }

  // ----- новый счётчик
  function renderMeterForm(gid) {
    var g = idx.garage[gid];
    if (!g) return renderGarage(gid);
    var m = meterState(g);
    view.innerHTML = formShell("Новый счётчик, гараж №" + esc(g.number),
      (m ? '<div class="alert alert-info py-2 small">Сейчас стоит счётчик ' + esc(m.number) +
        ". Если меняете его — сначала внесите последние показания старого: после замены их уже не внести.</div>" : "") +
      field("Номер счётчика", input("meter_number", 'autocomplete="off" required')) +
      field("Дата установки", input("installed_date", 'type="date" value="' + today() + '" max="' + today() + '"')) +
      field("Начальные показания", input("initial_reading", 'inputmode="decimal" autocomplete="off"')) +
      field("Пломба на счётчике", input("meter_seal_number", 'autocomplete="off"')) +
      field("Пломба на вводном автомате", input("breaker_seal_number", 'autocomplete="off"')) +
      field("Комментарий", input("comment", 'autocomplete="off"')),
      "#g/" + g.id);
    onSubmit(function (f) {
      var number = f.meter_number.value.trim();
      if (!number) fail("Укажите номер счётчика.");
      if (f.initial_reading.value && num(f.initial_reading.value) == null) fail("Начальные показания — числом.");
      return saveEntry("meter", { garage_id: g.id, meter_number: number, installed_date: f.installed_date.value,
        initial_reading: f.initial_reading.value ? String(num(f.initial_reading.value)) : "",
        meter_seal_number: f.meter_seal_number.value.trim(), breaker_seal_number: f.breaker_seal_number.value.trim(),
        comment: f.comment.value.trim() }, null, "#g/" + g.id);
    });
  }

  // ----- контрольные показания
  function renderControlForm(cid) {
    var c = idx.control[cid];
    if (!c) return renderControl();
    var last = num(c.last_reading), lastDate = c.last_date;
    localEntries().forEach(function (e) {
      if (e.kind === "control_reading" && e.payload.control_meter_id === c.id) { last = num(e.payload.value); lastDate = e.payload.date; }
    });
    view.innerHTML = formShell("Показания: " + esc(c.name),
      '<p class="mb-2">' + (last != null ? "Последнее: <strong>" + last + "</strong>" + (lastDate ? " от " + date(lastDate) : "") : "Показаний ещё нет") + "</p>" +
      field("Показания", input("value", 'inputmode="decimal" autocomplete="off" required style="font-size:1.4rem"')) +
      field("Дата снятия", input("date", 'type="date" value="' + today() + '" max="' + today() + '"')) +
      field("Комментарий", input("comment", 'autocomplete="off"')),
      "#control");
    var form = onSubmit(function (f) {
      var value = num(f.value.value);
      if (value == null) fail("Укажите показания.");
      if (last != null && value < last) fail("Показания не могут быть меньше последних (" + last + ").");
      return saveEntry("control_reading", { control_meter_id: c.id, value: String(value), date: f.date.value,
        comment: f.comment.value.trim() }, null, "#control");
    });
    form.value.focus();
  }

  // ----- оплата
  function paymentTargets(g) {
    var targets = [];
    if (g.meter || g.electricity_balance != null) {
      targets.push({ key: "electricity", label: "Электричество, гараж №" + g.number,
        balance: g.electricity_balance == null ? 0 : cents(g.electricity_balance) });
    }
    var seen = {};
    (idx.accountsByGarage[g.id] || []).forEach(function (a) { seen[a.id] = true; targets.push({ key: "account:" + a.id, label: accountLabel(a), balance: cents(a.balance) }); });
    g.owners.forEach(function (o) {
      (idx.accountsByPerson[o.person_id] || []).forEach(function (a) {
        if (a.garage_id == null && !seen[a.id]) { seen[a.id] = true; targets.push({ key: "account:" + a.id, label: accountLabel(a), balance: cents(a.balance) }); }
      });
    });
    return targets;
  }

  function renderPaymentForm(gid) {
    var g = idx.garage[gid];
    if (!g) return renderGarage(gid);
    var targets = paymentTargets(g);
    var payers = g.owners.map(function (o) { return o.person_id; }).concat(g.contacts.map(function (c) { return c.person_id; }))
      .filter(function (v, i, a) { return a.indexOf(v) === i && idx.person[v]; });
    var canPost = rec.data.user.can_post_payments;
    view.innerHTML = formShell("Оплата, гараж №" + esc(g.number),
      (canPost ? "" : '<div class="alert alert-info py-2 small">Оплату, принятую членом правления, проводит председатель или бухгалтер — после отправки она будет ждать их подтверждения.</div>') +
      field("За что", '<select class="form-select" name="target">' + targets.map(function (t) {
        return '<option value="' + t.key + '" data-balance="' + t.balance + '">' + esc(t.label) + (t.balance < 0 ? " — долг " + money(t.balance / 100) : t.balance > 0 ? " — переплата " + money(t.balance / 100) : "") + "</option>";
      }).join("") + "</select>") +
      field("Сумма, ₽", input("amount", 'inputmode="decimal" autocomplete="off" required style="font-size:1.4rem"')) +
      field("Дата", input("date", 'type="date" value="' + today() + '" max="' + today() + '"')) +
      field("Плательщик", '<select class="form-select" name="payer">' + payers.map(function (pid) {
        return '<option value="' + pid + '">' + esc(personName(pid)) + "</option>";
      }).join("") + '<option value="">другой человек…</option></select>' +
        '<input class="form-control mt-2 d-none" name="payer_name" placeholder="Кто платит" autocomplete="off">') +
      field("Комментарий", input("comment", 'autocomplete="off"')),
      "#g/" + g.id);
    if (!targets.length) {
      formError("У гаража нет счетов, на которые можно принять оплату.");
      return;
    }
    var form = onSubmit(function (f) {
      var amount = num(f.amount.value);
      if (amount == null || amount <= 0) fail("Укажите сумму больше нуля.");
      if (!f.payer.value && !f.payer_name.value.trim()) fail("Укажите, кто платит.");
      var target = f.target.value;
      return coopOffline.nextReceipt(rec.data.user).then(function (receipt) {
        var payload = { target: target === "electricity" ? "electricity" : "account", garage_id: g.id,
          account_id: target === "electricity" ? null : parseInt(target.split(":")[1], 10),
          amount: amount.toFixed(2), date: f.date.value, receipt: receipt,
          payer_person_id: f.payer.value ? parseInt(f.payer.value, 10) : null,
          payer_name: f.payer.value ? personName(parseInt(f.payer.value, 10)) : f.payer_name.value.trim(),
          comment: f.comment.value.trim() };
        return saveEntry("payment", payload, null, function (entry) { return "receipt/" + entry.uuid; });
      });
    });
    function prefill() {
      var opt = form.target.selectedOptions[0];
      var bal = opt ? parseInt(opt.getAttribute("data-balance"), 10) : 0;
      form.amount.placeholder = bal < 0 ? (Math.abs(bal) / 100).toFixed(2) : "";
    }
    form.target.addEventListener("change", prefill);
    form.payer.addEventListener("change", function () { form.payer_name.classList.toggle("d-none", !!form.payer.value); });
    form.amount.addEventListener("focus", function () { if (!form.amount.value && form.amount.placeholder) form.amount.value = form.amount.placeholder; });
    prefill();
  }

  function renderReceipt(id) {
    var e = queue.filter(function (x) { return x.uuid === id; })[0];
    if (!e) { view.innerHTML = '<p class="text-body-secondary">Квитанция не найдена.</p>'; return; }
    var p = e.payload;
    var what = p.target === "electricity" ? "Электроэнергия" : (accountById(p.account_id) ? (idx.feeType[accountById(p.account_id).fee_type_id] || { name: "Взнос" }).name + ", л/с " + accountById(p.account_id).number : "Взнос");
    var rows = [["Кооператив", rec.data.coop.name || ""], ["Дата", date(p.date)], ["Гараж", garageNo(p.garage_id)],
      ["Плательщик", p.payer_name || ""], ["Назначение", what], ["Принял", p.captured_by || rec.data.user.username]];
    view.innerHTML = '<div class="card border-2 border-success mb-3"><div class="card-body">' +
      '<div class="text-body-secondary">Квитанция</div><h3 class="mb-3">№ ' + esc(p.receipt) + "</h3>" +
      '<div class="display-6 fw-bold mb-3">' + money(num(p.amount)) + "</div>" +
      '<table class="table table-sm mb-2">' + rows.map(function (r) { return "<tr><th class=\"fw-normal text-body-secondary\">" + r[0] + "</th><td>" + esc(r[1]) + "</td></tr>"; }).join("") + "</table>" +
      (p.comment ? '<div class="small">' + esc(p.comment) + "</div>" : "") +
      '<div class="small text-body-secondary mt-2">Принято наличными. Сфотографируйте экран — это подтверждение оплаты. ' +
      (e.state === "local" ? "Оплата попадёт на счёт, когда телефон отправит записи на сайт." : "Отправлено на сайт.") + "</div>" +
      "</div></div>" +
      '<a class="btn btn-primary w-100" href="#g/' + p.garage_id + '">Готово</a>';
  }

  // ----- человек: новый и правка
  var FIELD_NAMES = { full_name: "ФИО", phones: "телефоны", email: "почта", telegram: "Telegram", max_messenger: "MAX", comment: "комментарий" };
  function linkFields(gid, fixed) {
    var g = gid ? idx.garage[gid] : null;
    var hasOwner = g && g.owners.length;
    return '<fieldset class="border rounded p-3 mb-3"><legend class="fs-6 w-auto px-1 mb-0">Гараж</legend>' +
      (fixed ? '<input type="hidden" name="garage_number" value="' + esc(g.number) + '">'
        : field("Номер гаража", input("garage_number", 'autocomplete="off" inputmode="text" value="' + (g ? esc(g.number) : "") + '"'), "Пусто — не привязывать к гаражу.")) +
      field("Кем приходится гаражу", '<select class="form-select" name="role">' +
        '<option value="owner"' + (hasOwner ? "" : " selected") + '>собственник (гараж без собственника)</option>' +
        '<option value="replace"' + (hasOwner ? " selected" : "") + ">новый собственник вместо прежнего (продажа)</option>" +
        '<option value="coowner">совладелец</option>' +
        '<option value="contact">лицо для связи / арендатор</option></select>',
        "Продажу (смену собственника) председатель оформит на сайте — после отправки запись будет ждать его.") +
      '<div data-for="coowner" class="d-none">' + field("Доля", input("share", 'inputmode="decimal" placeholder="0.5" autocomplete="off"')) + "</div>" +
      '<div data-for="contact" class="d-none">' + field("Кем приходится", input("relation", 'placeholder="супруга, сын, доверенное лицо…" autocomplete="off"')) +
      '<div class="form-check mb-3"><input class="form-check-input" type="checkbox" name="tenant" id="olTenant"><label class="form-check-label" for="olTenant">арендатор</label></div></div>' +
      "</fieldset>";
  }
  function wireLink(form) {
    function sync() {
      form.querySelectorAll("[data-for]").forEach(function (el) { el.classList.toggle("d-none", el.getAttribute("data-for") !== form.role.value); });
    }
    form.role.addEventListener("change", sync);
    sync();
  }
  function readLink(form) {
    var number = form.garage_number.value.trim();
    if (!number) return null;
    var g = rec.data.garages.filter(function (x) { return norm(x.number) === norm(number); })[0];
    if (!g) fail("Гараж №" + number + " не найден.");
    var link = { garage_id: g.id, role: form.role.value };
    if (link.role === "coowner") {
      var share = num(form.share.value);
      if (share == null || share <= 0 || share > 1) fail("Доля — число больше 0 и не больше 1 (например 0.5).");
      link.share = String(share);
    }
    if (link.role === "contact") { link.relation = form.relation.value.trim(); link.tenant = form.tenant.checked; }
    if (link.role === "owner" && g.owners.length) fail("У гаража №" + g.number + " уже есть собственник — выберите «вместо прежнего» или «совладелец».");
    return link;
  }
  function phonesFrom(text) { return text.split(/[,;\n]/).map(function (s) { return s.trim(); }).filter(Boolean); }

  function renderPersonForm(gid) {
    view.innerHTML = formShell("Новый человек",
      field("Фамилия, имя, отчество", input("full_name", 'autocomplete="off" autocapitalize="words" required')) +
      field("Телефоны", '<textarea class="form-control" name="phones" rows="2" inputmode="tel" placeholder="по одному в строке"></textarea>') +
      linkFields(gid) +
      '<details class="mb-3"><summary>Паспорт, адрес и прочее</summary><div class="pt-3">' +
      field("Почта", input("email", 'type="email" autocomplete="off"')) +
      field("Telegram", input("telegram", 'autocomplete="off"')) +
      field("MAX", input("max_messenger", 'autocomplete="off"')) +
      field("Адрес регистрации", input("registration_address", 'autocomplete="off"')) +
      field("Адрес проживания", input("residence_address", 'autocomplete="off"')) +
      field("Дата рождения", input("birth_date", 'type="date"')) +
      '<div class="row g-2"><div class="col-4">' + field("Серия", input("passport_series", 'inputmode="numeric" maxlength="4" autocomplete="off"')) + "</div>" +
      '<div class="col-8">' + field("Номер паспорта", input("passport_number", 'inputmode="numeric" maxlength="6" autocomplete="off"')) + "</div></div>" +
      field("Дата выдачи", input("passport_issue_date", 'type="date"')) +
      field("Член кооператива с", input("membership_start_date", 'type="date"')) +
      "</div></details>" +
      field("Комментарий", '<textarea class="form-control" name="comment" rows="2"></textarea>'),
      gid ? "#g/" + gid : "#");
    var form = onSubmit(function (f) {
      var name = f.full_name.value.trim().replace(/\s+/g, " ");
      if (!name) fail("Укажите фамилию, имя и отчество.");
      var link = readLink(f);
      var fields = { full_name: name };
      ["email", "telegram", "max_messenger", "registration_address", "residence_address", "birth_date",
        "passport_series", "passport_number", "passport_issue_date", "membership_start_date", "comment"].forEach(function (k) {
        if (f[k].value.trim()) fields[k] = f[k].value.trim();
      });
      var dup = rec.data.persons.filter(function (p) { return norm(p.name) === norm(name); })[0];
      if (dup && !window.confirm("«" + dup.name + "» уже есть в реестре. Всё равно внести нового человека?")) return;
      return saveEntry("person_create", { fields: fields, phones: phonesFrom(f.phones.value), link: link }, null,
        link ? "#g/" + link.garage_id : "#queue");
    });
    wireLink(form);
    form.full_name.focus();
  }

  function renderLinkForm(gid) {
    var g = idx.garage[gid];
    if (!g) return renderGarage(gid);
    view.innerHTML = formShell("Привязать к гаражу №" + esc(g.number),
      field("Кто", input("who", 'autocomplete="off" placeholder="фамилия или телефон"') + '<div class="list-group mt-2" id="olWho"></div>') +
      '<input type="hidden" name="person_id">' + linkFields(gid, true) +
      '<p class="small"><a href="#f/person/' + g.id + '">Нет в реестре — новый человек</a></p>',
      "#g/" + g.id);
    var form = onSubmit(function (f) {
      if (!f.person_id.value) fail("Выберите человека из списка.");
      var link = readLink(f);
      return saveEntry("link", { person_id: parseInt(f.person_id.value, 10), link: link }, null, "#g/" + g.id);
    });
    wireLink(form);
    var list = document.getElementById("olWho");
    form.who.addEventListener("input", function () {
      var r = search(form.who.value).persons.slice(0, 8);
      list.innerHTML = r.map(function (p) {
        return '<button type="button" class="list-group-item list-group-item-action" data-pid="' + p.id + '">' + esc(p.name) + "</button>";
      }).join("");
    });
    list.addEventListener("click", function (evt) {
      var b = evt.target.closest("[data-pid]");
      if (!b) return;
      form.person_id.value = b.getAttribute("data-pid");
      form.who.value = personName(parseInt(form.person_id.value, 10));
      list.innerHTML = "";
    });
    form.who.focus();
  }

  function renderEditForm(pid) {
    var p = idx.person[pid];
    if (!p) return renderPerson(pid);
    var phones = p.phones.map(function (ph) { return ph.number; });
    view.innerHTML = formShell("Правка: " + esc(p.name),
      field("Фамилия, имя, отчество", input("full_name", 'autocomplete="off" value="' + esc(p.name) + '"')) +
      field("Телефоны", '<textarea class="form-control" name="phones" rows="2" inputmode="tel">' + esc(phones.join("\n")) + "</textarea>", "по одному в строке") +
      field("Почта", input("email", 'type="email" autocomplete="off" value="' + esc(p.email || "") + '"')) +
      field("Telegram", input("telegram", 'autocomplete="off" value="' + esc(p.telegram || "") + '"')) +
      field("MAX", input("max_messenger", 'autocomplete="off" value="' + esc(p.max || "") + '"')) +
      field("Комментарий", '<textarea class="form-control" name="comment" rows="3">' + esc(p.comment || "") + "</textarea>"),
      "#p/" + p.id);
    onSubmit(function (f) {
      var old = { full_name: p.name, email: p.email || "", telegram: p.telegram || "", max_messenger: p.max || "", comment: p.comment || "" };
      var changes = {};
      Object.keys(old).forEach(function (k) {
        var v = f[k].value.trim();
        if (k === "full_name") v = v.replace(/\s+/g, " ");
        if (v !== old[k].trim()) changes[k] = [old[k], v];
      });
      var newPhones = phonesFrom(f.phones.value);
      if (newPhones.join("|") !== phones.join("|")) changes.phones = [phones, newPhones];
      if (!Object.keys(changes).length) { location.hash = "p/" + p.id; return; }
      if (changes.full_name && !changes.full_name[1]) fail("ФИО не может быть пустым.");
      return saveEntry("person_edit", { person_id: p.id, changes: changes }, null, "#p/" + p.id);
    });
  }

  // Кнопки действий на карточках.
  function garageActions(g) {
    return '<div class="d-grid gap-2 mb-3" style="grid-template-columns: 1fr 1fr">' +
      '<a class="btn btn-primary" href="#f/reading/' + g.id + '"><i class="bi bi-speedometer2" aria-hidden="true"></i> Показания</a>' +
      '<a class="btn btn-success" href="#f/pay/' + g.id + '"><i class="bi bi-cash-coin" aria-hidden="true"></i> Оплата</a>' +
      '<a class="btn btn-outline-secondary btn-sm" href="#f/meter/' + g.id + '">Новый счётчик</a>' +
      '<a class="btn btn-outline-secondary btn-sm" href="#f/link/' + g.id + '">Собственник / контакт</a>' +
      "</div>" + queueSection(function (e) { return entryGarageId(e) === g.id; }, "Записи по гаражу без сети");
  }
  function personActions(p) {
    var garages = (idx.personGarages[p.id] || []).filter(function (pg) { return pg.role === "owner"; });
    return '<div class="d-flex flex-wrap gap-2 mb-3">' +
      '<a class="btn btn-outline-primary btn-sm" href="#f/edit/' + p.id + '"><i class="bi bi-pencil" aria-hidden="true"></i> Изменить данные</a>' +
      garages.map(function (pg) { return '<a class="btn btn-outline-success btn-sm" href="#f/pay/' + pg.garage.id + '">Оплата, №' + esc(pg.garage.number) + "</a>"; }).join("") +
      "</div>" + queueSection(function (e) { return e.payload && e.payload.person_id === p.id; }, "Записи без сети");
  }

  // ------------------------------------------------- контрольные счётчики
  function renderControl() {
    var list = rec.data.control_meters;
    if (!list.length) { view.innerHTML = '<p class="text-body-secondary">Контрольных счётчиков нет.</p>'; return; }
    var children = {};
    list.forEach(function (c) { var k = c.parent_id == null ? "root" : c.parent_id; (children[k] = children[k] || []).push(c); });
    var html = '<div class="list-group ol-list">';
    function walk(key, depth) {
      (children[key] || []).forEach(function (c) {
        html += '<div class="list-group-item ol-tree-' + Math.min(depth, 3) + '">' +
          '<div class="d-flex justify-content-between align-items-center gap-2"><span>' + esc(c.name) + '</span><span class="text-nowrap">' +
          (c.last_reading != null ? "<strong>" + esc(c.last_reading) + "</strong>" : '<span class="text-body-secondary">нет показаний</span>') +
          ' <a class="btn btn-sm btn-outline-primary ms-2" href="#f/control/' + c.id + '">Внести</a></span></div>' +
          '<div class="small text-body-secondary">' +
          [c.last_date ? "от " + date(c.last_date) : "", c.parent_garage ? "через щиток гаража №" + esc(c.parent_garage) : "", esc(c.comment || "")]
            .filter(Boolean).join(" · ") + "</div></div>";
        walk(c.id, depth + 1);
      });
    }
    walk("root", 0);
    html += "</div>";
    view.innerHTML = html;
  }

  // ------------------------------------------------------------- маршруты
  function route() {
    var h = location.hash.replace(/^#/, "");
    var tab = "search";
    if (h === "debtors") tab = "debtors";
    else if (h === "control") tab = "control";
    else if (h === "map" || /^map\//.test(h)) tab = "map";
    else if (h === "wiki" || /^w\//.test(h)) tab = "wiki";
    var card = /^([gpo]|f|receipt)\//.test(h) || h === "queue" || h === "f/person";
    tabs.querySelectorAll(".nav-link").forEach(function (a) {
      a.classList.toggle("active", a.getAttribute("data-tab") === tab && !card);
    });
    if (!rec) return;
    var m;
    if ((m = /^f\/(reading|pay|meter|link|person|edit|control)(?:\/(\d+))?$/.exec(h))) {
      var fid = m[2] ? parseInt(m[2], 10) : null;
      ({ reading: renderReadingForm, pay: renderPaymentForm, meter: renderMeterForm, link: renderLinkForm,
         person: renderPersonForm, edit: renderEditForm, control: renderControlForm })[m[1]](fid);
    }
    else if (h === "queue") renderQueue();
    else if ((m = /^receipt\/([\w-]+)$/.exec(h))) renderReceipt(m[1]);
    else if ((m = /^g\/(\d+)$/.exec(h))) renderGarage(parseInt(m[1], 10));
    else if ((m = /^p\/(\d+)$/.exec(h))) renderPerson(parseInt(m[1], 10));
    else if ((m = /^o\/(\d+)$/.exec(h))) renderObject(parseInt(m[1], 10));
    else if ((m = /^w\/(\d+)$/.exec(h))) renderWikiPage(parseInt(m[1], 10));
    else if ((m = /^map\/([go])\/(\d+)$/.exec(h))) renderMap({ kind: m[1], id: parseInt(m[2], 10) });
    else if (tab === "map") renderMap(null);
    else if (tab === "wiki") renderWikiList();
    else if (tab === "debtors") renderDebtors();
    else if (tab === "control") renderControl();
    else renderSearch();
    if (card || /^w\//.test(h)) window.scrollTo(0, 0);
  }
  window.addEventListener("hashchange", route);

  function renderEmpty() {
    tabs.classList.add("d-none");
    view.innerHTML = '<div class="card"><div class="card-body">' +
      '<h5 class="card-title">Данных на телефоне ещё нет</h5>' +
      '<p>Откройте эту страницу там, где есть обычный интернет (дома по Wi-Fi), войдя на сайт под учётной записью правления, и нажмите «Загрузить». ' +
      "Потом данные будут обновляться сами при каждом открытии сайта.</p>" +
      '<button type="button" class="btn btn-primary" id="olLoad">Загрузить</button> ' +
      '<a class="btn btn-outline-secondary" href="/auth/login?next=/offline/">Войти</a></div></div>';
    document.getElementById("olLoad").addEventListener("click", doRefresh);
  }

  function start(note) {
    return coopOffline.load().then(function (r) {
      if (r && coopOffline.isExpired(r)) {
        return coopOffline.wipe().then(function () { return null; });
      }
      return r;
    }).then(function (r) {
      rec = r;
      idx = r ? buildIndex(r.data) : null;
      renderStamp();
      renderBanner(note);
      if (!rec) { renderEmpty(); return loadQueue(); }
      coopOffline.mediaStatus().then(function (st) {
        if (st.saved >= st.total) return;
        var div = document.createElement("div");
        div.className = "alert alert-secondary py-2 small";
        div.textContent = "Фото и картинки сохранены не все: " + st.saved + " из " + st.total +
          ". Остальные докачаются, когда сайт будет открыт с интернетом.";
        banner.appendChild(div);
      });
      tabs.classList.remove("d-none");
      return loadQueue().then(route);
    });
  }

  start("").then(function () {
    // Открыли с сетью — тихо проверим свежесть (не чаще раза в 10 минут).
    if (location.pathname.replace(/\/+$/, "") === "/offline" && navigator.onLine !== false) {
      coopOffline.refresh().then(function (result) {
        if (result === "updated") start("");
        else if (rec) loadQueue().then(function () { if (/^(queue|[gp]\/)/.test(location.hash.slice(1))) route(); });
      });
    }
  });
})();
