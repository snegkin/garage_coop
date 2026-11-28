/*
 * Оффлайн-режим правления: слепок базы (app/offline.py) и очередь записей,
 * сделанных без сети (app/offline_sync.py), — хранение в IndexedDB и обмен
 * с сервером. Подключается в base.html (для правления — фоновое обновление
 * при открытии страниц сайта) и на оффлайн-странице /offline/.
 *
 * window.coopOffline:
 *   load()            → Promise<{etag, fetchedAt, data} | null>
 *   refresh({force})  → Promise<"updated" | "unchanged" | "skipped" | "login" | "denied" | "failed">
 *                       сначала отправляет очередь, потом обновляет слепок
 *   wipe()            → Promise — стереть слепок и картинки (выход из системы);
 *                       НЕотправленные записи остаются — в них может быть
 *                       принятая наличными оплата
 *   isExpired(record) → слепок старше MAX_AGE_DAYS — не показывать
 *   mediaStatus()     → Promise<{total, saved}> — сколько картинок слепка уже на телефоне
 *   queueAdd(kind, payload, photoBlob) → Promise<entry>
 *   queueList()       → Promise<entry[]> (по порядку записи)
 *   queueRemove(uuid) → Promise — удалить НЕотправленную запись (ошиблись)
 *   sendQueue()       → Promise<{sent, result}> — отправить неотправленное
 *   nextReceipt(user) → Promise<"<id пользователя>-<номер>"> — номер квитанции
 *
 * Запись очереди: {uuid, kind, payload, captured_at, seq, photo (Blob),
 * state: "local" | "sent", result: {status, message}, sent_at}. После
 * отправки фото и паспортные данные из записи стираются, сама она хранится
 * ещё SENT_KEEP_DAYS для просмотра итога.
 *
 * Картинки (data.media: подложка схемы, фото оборудования, картинки вики)
 * качаются после каждой проверки слепка в Cache Storage (MEDIA_CACHE) —
 * только недостающие; лишние (удалённые на сайте) убираются. Отдаёт их
 * оттуда service worker (main.py) — и на оффлайн-странице, и на сайте.
 */
(function () {
  "use strict";
  var DB_NAME = "coop-offline";
  var DB_VERSION = 2;
  var KV = "kv";
  var QUEUE = "queue";
  var KEY = "snapshot";
  var SNAPSHOT_URL = "/offline/snapshot.json";
  var SNAPSHOT_FORMAT = 3;          // app/offline.py: SNAPSHOT_FORMAT
  var MEDIA_CACHE = "coop-media";   // main.py: MEDIA_CACHE (не «coop-offline-…» — тот чистит worker при обновлении)
  var MEDIA_PARALLEL = 3;
  var CHECK_INTERVAL_MS = 10 * 60 * 1000;
  var FETCH_TIMEOUT_MS = 20000;     // при белых списках запрос может просто висеть
  var SEND_TIMEOUT_MS = 120000;     // отправка с фото — дольше
  var MAX_AGE_DAYS = 30;
  var SENT_KEEP_DAYS = 14;
  var CHECKED_AT_KEY = "coopOfflineCheckedAt";
  var SECRET_FIELDS = ["passport_series", "passport_number", "passport_issue_date", "birth_date",
    "registration_address", "residence_address"];

  function openDb() {
    return new Promise(function (resolve, reject) {
      if (!("indexedDB" in window)) { reject(new Error("no indexedDB")); return; }
      var req = indexedDB.open(DB_NAME, DB_VERSION);
      req.onupgradeneeded = function () {
        var db = req.result;
        if (!db.objectStoreNames.contains(KV)) db.createObjectStore(KV);
        if (!db.objectStoreNames.contains(QUEUE)) db.createObjectStore(QUEUE, { keyPath: "uuid" });
      };
      req.onsuccess = function () { resolve(req.result); };
      req.onerror = function () { reject(req.error); };
    });
  }

  // Транзакция по хранилищу name: fn(store) может вернуть IDBRequest — его
  // результат и вернётся после завершения транзакции.
  function tx(name, mode, fn) {
    return openDb().then(function (db) {
      return new Promise(function (resolve, reject) {
        var t = db.transaction(name, mode);
        var req;
        t.oncomplete = function () { db.close(); resolve(req && "result" in req ? req.result : undefined); };
        t.onerror = t.onabort = function () { db.close(); reject(t.error); };
        req = fn(t.objectStore(name));
      });
    });
  }

  function kvGet(key) { return tx(KV, "readonly", function (s) { return s.get(key); }); }
  function kvPut(key, value) { return tx(KV, "readwrite", function (s) { s.put(value, key); }); }

  function load() {
    return kvGet(KEY).then(function (rec) {
      if (rec && (!rec.data || rec.data.format !== SNAPSHOT_FORMAT)) return null;
      return rec || null;
    }).catch(function () { return null; });
  }

  function save(rec) { return kvPut(KEY, rec); }

  // Слепок и картинки — стереть; неотправленные записи — оставить.
  function wipe() {
    try { localStorage.removeItem(CHECKED_AT_KEY); } catch (e) {}
    var media = ("caches" in window) ? caches.delete(MEDIA_CACHE).catch(function () {}) : Promise.resolve();
    return media.then(function () {
      return tx(KV, "readwrite", function (s) { s.delete(KEY); }).catch(function () {});
    }).then(function () {
      return tx(QUEUE, "readwrite", function (s) {
        var req = s.openCursor();
        req.onsuccess = function () {
          var cur = req.result;
          if (!cur) return;
          if (cur.value.state !== "local") cur.delete();
          cur.continue();
        };
      }).catch(function () {});
    });
  }

  function absolute(url) { return new URL(url, location.origin).href; }

  function mediaStatus() {
    return load().then(function (rec) {
      var urls = (rec && rec.data.media) || [];
      if (!urls.length || !("caches" in window)) return { total: urls.length, saved: 0 };
      return caches.open(MEDIA_CACHE).then(function (cache) { return cache.keys(); }).then(function (keys) {
        var have = {};
        keys.forEach(function (r) { have[r.url] = true; });
        return { total: urls.length, saved: urls.filter(function (u) { return have[absolute(u)]; }).length };
      });
    }).catch(function () { return { total: 0, saved: 0 }; });
  }

  // Докачать недостающие картинки слепка и убрать лишние. Ответ годится, только
  // если это сам файл: истекла сессия — сайт отвечает переадресацией на вход.
  function syncMedia(urls) {
    if (!("caches" in window)) return Promise.resolve();
    var want = {};
    urls.forEach(function (u) { want[absolute(u)] = true; });
    return caches.open(MEDIA_CACHE).then(function (cache) {
      return cache.keys().then(function (keys) {
        var have = {};
        keys.forEach(function (r) {
          if (want[r.url]) have[r.url] = true;
          else cache.delete(r);
        });
        var queue = urls.filter(function (u) { return !have[absolute(u)]; });
        function worker() {
          var url = queue.shift();
          if (!url) return Promise.resolve();
          return fetchWithTimeout(url, { credentials: "same-origin" }).then(function (resp) {
            var type = resp.headers.get("Content-Type") || "";
            if (resp.ok && !resp.redirected && type.indexOf("text/html") !== 0) return cache.put(absolute(url), resp);
          }).catch(function () {}).then(worker);
        }
        var workers = [];
        for (var i = 0; i < MEDIA_PARALLEL; i++) workers.push(worker());
        return Promise.all(workers);
      });
    }).catch(function () {});
  }

  function isExpired(rec) {
    if (!rec || !rec.fetchedAt) return true;
    return Date.now() - rec.fetchedAt > MAX_AGE_DAYS * 24 * 3600 * 1000;
  }

  function checkedRecently() {
    try {
      var at = parseInt(localStorage.getItem(CHECKED_AT_KEY) || "0", 10);
      return Date.now() - at < CHECK_INTERVAL_MS;
    } catch (e) { return false; }
  }

  function markChecked() {
    try { localStorage.setItem(CHECKED_AT_KEY, String(Date.now())); } catch (e) {}
  }

  function fetchWithTimeout(url, options, timeoutMs) {
    var ctrl = ("AbortController" in window) ? new AbortController() : null;
    if (ctrl) options.signal = ctrl.signal;
    var timer = setTimeout(function () { if (ctrl) ctrl.abort(); }, timeoutMs || FETCH_TIMEOUT_MS);
    return fetch(url, options).finally(function () { clearTimeout(timer); });
  }

  // ------------------------------------------------------------------ очередь
  function uuid() {
    if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
    var b = new Uint8Array(16);
    crypto.getRandomValues(b);
    b[6] = (b[6] & 0x0f) | 0x40; b[8] = (b[8] & 0x3f) | 0x80;
    var h = Array.prototype.map.call(b, function (x) { return (x + 0x100).toString(16).slice(1); }).join("");
    return h.slice(0, 8) + "-" + h.slice(8, 12) + "-" + h.slice(12, 16) + "-" + h.slice(16, 20) + "-" + h.slice(20);
  }

  function localIso(d) {
    var pad = function (n) { return (n < 10 ? "0" : "") + n; };
    return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()) + "T" +
      pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds());
  }

  function queueList() {
    return tx(QUEUE, "readonly", function (s) { return s.getAll(); }).then(function (list) {
      return (list || []).sort(function (a, b) { return a.seq - b.seq; });
    }).catch(function () { return []; });
  }

  function queuePut(entry) { return tx(QUEUE, "readwrite", function (s) { s.put(entry); }); }

  function queueAdd(kind, payload, photo) {
    var now = new Date();
    var entry = {
      uuid: uuid(), kind: kind, payload: payload, captured_at: localIso(now), seq: now.getTime(),
      photo: photo || null, state: "local", result: null, sent_at: null,
    };
    return queuePut(entry).then(function () {
      if (navigator.storage && navigator.storage.persist) navigator.storage.persist().catch(function () {});
      return entry;
    });
  }

  function queueRemove(id) {
    return tx(QUEUE, "readwrite", function (s) {
      var req = s.get(id);
      req.onsuccess = function () { if (req.result && req.result.state === "local") s.delete(id); };
    });
  }

  function nextReceipt(user) {
    return kvGet("receiptSeq").then(function (local) {
      var seq = Math.max(local || 0, (user && user.receipt_seq) || 0) + 1;
      return kvPut("receiptSeq", seq).then(function () {
        return (user ? user.id : "0") + "-" + String(seq).padStart(4, "0");
      });
    });
  }

  function cleanupSent(list) {
    var limit = Date.now() - SENT_KEEP_DAYS * 86400000;
    var old = list.filter(function (e) { return e.state === "sent" && e.sent_at && e.sent_at < limit; });
    if (!old.length) return Promise.resolve();
    return tx(QUEUE, "readwrite", function (s) { old.forEach(function (e) { s.delete(e.uuid); }); });
  }

  // Отправить неотправленное. result: "nothing" | "sent" | "login" | "denied" | "failed".
  var sending = null;
  function sendQueue() {
    if (sending) return sending;
    sending = queueList().then(function (list) {
      return cleanupSent(list).then(function () {
        var local = list.filter(function (e) { return e.state === "local"; });
        if (!local.length) return { sent: 0, result: "nothing" };
        if (navigator.onLine === false) return { sent: 0, result: "failed" };
        return fetchWithTimeout("/offline/csrf", { credentials: "same-origin", cache: "no-store" }).then(function (resp) {
          if (resp.status === 401 || resp.redirected) return { sent: 0, result: "login" };
          if (!resp.ok) return { sent: 0, result: "failed" };
          return resp.json().then(function (tok) {
            var form = new FormData();
            form.append("entries", JSON.stringify(local.map(function (e) {
              return { uuid: e.uuid, kind: e.kind, captured_at: e.captured_at, payload: e.payload };
            })));
            local.forEach(function (e) { if (e.photo) form.append("photo_" + e.uuid, e.photo, "photo.jpg"); });
            return fetchWithTimeout("/offline/sync", {
              method: "POST", body: form, credentials: "same-origin", headers: { "X-CSRFToken": tok.token },
            }, SEND_TIMEOUT_MS).then(function (resp) {
              if (resp.status === 401) return { sent: 0, result: "login" };
              if (resp.status === 403) return { sent: 0, result: "denied" };
              if (!resp.ok) return { sent: 0, result: "failed" };
              return resp.json().then(function (data) {
                var byId = {};
                (data.results || []).forEach(function (r) { byId[r.uuid] = r; });
                var done = local.filter(function (e) { return byId[e.uuid]; });
                return tx(QUEUE, "readwrite", function (s) {
                  done.forEach(function (e) {
                    e.state = "sent";
                    e.sent_at = Date.now();
                    e.result = { status: byId[e.uuid].status, message: byId[e.uuid].message };
                    e.photo = null;
                    if (e.payload && e.payload.fields) SECRET_FIELDS.forEach(function (k) { delete e.payload.fields[k]; });
                    s.put(e);
                  });
                }).then(function () {
                  return data.receipt_seq ? kvGet("receiptSeq").then(function (v) {
                    if ((v || 0) < data.receipt_seq) return kvPut("receiptSeq", data.receipt_seq);
                  }) : null;
                }).then(function () { return { sent: done.length, result: "sent" }; });
              });
            });
          });
        });
      });
    }).catch(function () { return { sent: 0, result: "failed" }; }).finally(function () { sending = null; });
    return sending;
  }

  // ------------------------------------------------------------------ слепок
  function refresh(opts) {
    opts = opts || {};
    if (navigator.onLine === false) return Promise.resolve("failed");
    return sendQueue().then(function (sent) {
      if (sent.result === "login") return "login";
      if (sent.result === "denied") return wipe().then(function () { return "denied"; });
      // Отправили записи — слепок мог измениться, проверяем без паузы.
      var force = opts.force || sent.sent > 0;
      if (!force && checkedRecently()) return "skipped";
      return fetchSnapshot();
    });
  }

  function fetchSnapshot() {
    return load().then(function (rec) {
      var headers = { "Accept": "application/json" };
      if (rec && rec.etag && !isExpired(rec)) headers["If-None-Match"] = '"' + rec.etag + '"';
      return fetchWithTimeout(SNAPSHOT_URL, { credentials: "same-origin", headers: headers, cache: "no-store" })
        .then(function (resp) {
          // Истекла сессия — данные не трогаем, просим войти; не правление — стираем.
          if (resp.status === 401) return "login";
          if (resp.status === 403) return wipe().then(function () { return "denied"; });
          var etag = (resp.headers.get("ETag") || "").replace(/^W\//, "").replace(/"/g, "");
          if (resp.status === 304 && rec) {
            rec.fetchedAt = Date.now();
            return save(rec).then(function () {
              markChecked();
              return syncMedia(rec.data.media || []).then(function () { return "unchanged"; });
            });
          }
          if (!resp.ok) return "failed";
          return resp.json().then(function (data) {
            return save({ etag: etag, fetchedAt: Date.now(), data: data }).then(function () {
              markChecked();
              // Без этого Safari может стереть данные сайта при нехватке места.
              if (navigator.storage && navigator.storage.persist) {
                navigator.storage.persist().catch(function () {});
              }
              return syncMedia(data.media || []).then(function () { return "updated"; });
            });
          });
        });
    }).catch(function () { return "failed"; });
  }

  window.coopOffline = {
    load: load, refresh: refresh, wipe: wipe, isExpired: isExpired, mediaStatus: mediaStatus,
    queueAdd: queueAdd, queueList: queueList, queueRemove: queueRemove, sendQueue: sendQueue,
    nextReceipt: nextReceipt, MAX_AGE_DAYS: MAX_AGE_DAYS,
  };
})();
