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
      '<div id="olResults"></div>';
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
      '<div class="mb-3">' + contactsHtml(p) + "</div>";

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
          '<div class="d-flex justify-content-between gap-2"><span>' + esc(c.name) + "</span><span>" +
          (c.last_reading != null ? "<strong>" + esc(c.last_reading) + "</strong>" : '<span class="text-body-secondary">нет показаний</span>') + "</span></div>" +
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
    var card = /^[gpo]\//.test(h);
    tabs.querySelectorAll(".nav-link").forEach(function (a) {
      a.classList.toggle("active", a.getAttribute("data-tab") === tab && !card);
    });
    if (!rec) return;
    var m;
    if ((m = /^g\/(\d+)$/.exec(h))) renderGarage(parseInt(m[1], 10));
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
      if (!rec) { renderEmpty(); return; }
      coopOffline.mediaStatus().then(function (st) {
        if (st.saved >= st.total) return;
        var div = document.createElement("div");
        div.className = "alert alert-secondary py-2 small";
        div.textContent = "Фото и картинки сохранены не все: " + st.saved + " из " + st.total +
          ". Остальные докачаются, когда сайт будет открыт с интернетом.";
        banner.appendChild(div);
      });
      tabs.classList.remove("d-none");
      route();
    });
  }

  start("").then(function () {
    // Открыли с сетью — тихо проверим свежесть (не чаще раза в 10 минут).
    if (location.pathname.replace(/\/+$/, "") === "/offline" && navigator.onLine !== false) {
      coopOffline.refresh().then(function (result) { if (result === "updated") start(""); });
    }
  });
})();
