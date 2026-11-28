/*
 * Оффлайн-слепок базы для правления (app/offline.py): хранение в IndexedDB
 * и обновление с сервера. Подключается в base.html (для правления — фоновое
 * обновление при открытии страниц сайта) и на оффлайн-странице /offline/.
 *
 * window.coopOffline:
 *   load()            → Promise<{etag, fetchedAt, data} | null>
 *   refresh({force})  → Promise<"updated" | "unchanged" | "skipped" | "denied" | "failed">
 *   wipe()            → Promise — стереть слепок (выход из системы, отказ сервера)
 *   isExpired(record) → слепок старше MAX_AGE_DAYS — не показывать
 *   mediaStatus()     → Promise<{total, saved}> — сколько картинок слепка уже на телефоне
 *
 * Картинки (data.media: подложка схемы, фото оборудования, картинки вики)
 * качаются после каждой проверки слепка в Cache Storage (MEDIA_CACHE) —
 * только недостающие; лишние (удалённые на сайте) убираются. Отдаёт их
 * оттуда service worker (main.py) — и на оффлайн-странице, и на сайте.
 */
(function () {
  "use strict";
  var DB_NAME = "coop-offline";
  var STORE = "kv";
  var KEY = "snapshot";
  var SNAPSHOT_URL = "/offline/snapshot.json";
  var SNAPSHOT_FORMAT = 2;          // app/offline.py: SNAPSHOT_FORMAT
  var MEDIA_CACHE = "coop-media";   // main.py: MEDIA_CACHE (не «coop-offline-…» — тот чистит worker при обновлении)
  var MEDIA_PARALLEL = 3;
  var CHECK_INTERVAL_MS = 10 * 60 * 1000;
  var FETCH_TIMEOUT_MS = 20000;     // при белых списках запрос может просто висеть
  var MAX_AGE_DAYS = 30;
  var CHECKED_AT_KEY = "coopOfflineCheckedAt";

  function openDb() {
    return new Promise(function (resolve, reject) {
      if (!("indexedDB" in window)) { reject(new Error("no indexedDB")); return; }
      var req = indexedDB.open(DB_NAME, 1);
      req.onupgradeneeded = function () {
        var db = req.result;
        if (!db.objectStoreNames.contains(STORE)) db.createObjectStore(STORE);
      };
      req.onsuccess = function () { resolve(req.result); };
      req.onerror = function () { reject(req.error); };
    });
  }

  function tx(mode, fn) {
    return openDb().then(function (db) {
      return new Promise(function (resolve, reject) {
        var t = db.transaction(STORE, mode);
        var result;
        t.oncomplete = function () { db.close(); resolve(result); };
        t.onerror = t.onabort = function () { db.close(); reject(t.error); };
        result = fn(t.objectStore(STORE));
      });
    });
  }

  function load() {
    return openDb().then(function (db) {
      return new Promise(function (resolve, reject) {
        var req = db.transaction(STORE, "readonly").objectStore(STORE).get(KEY);
        req.onsuccess = function () {
          db.close();
          var rec = req.result || null;
          if (rec && (!rec.data || rec.data.format !== SNAPSHOT_FORMAT)) rec = null;
          resolve(rec);
        };
        req.onerror = function () { db.close(); reject(req.error); };
      });
    }).catch(function () { return null; });
  }

  function save(rec) {
    return tx("readwrite", function (store) { store.put(rec, KEY); });
  }

  function wipe() {
    try { localStorage.removeItem(CHECKED_AT_KEY); } catch (e) {}
    var media = ("caches" in window) ? caches.delete(MEDIA_CACHE).catch(function () {}) : Promise.resolve();
    return media.then(function () { return new Promise(function (resolve) {
      if (!("indexedDB" in window)) { resolve(); return; }
      var req = indexedDB.deleteDatabase(DB_NAME);
      req.onsuccess = req.onerror = req.onblocked = function () { resolve(); };
    }); });
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

  function fetchWithTimeout(url, options) {
    var ctrl = ("AbortController" in window) ? new AbortController() : null;
    if (ctrl) options.signal = ctrl.signal;
    var timer = setTimeout(function () { if (ctrl) ctrl.abort(); }, FETCH_TIMEOUT_MS);
    return fetch(url, options).finally(function () { clearTimeout(timer); });
  }

  function refresh(opts) {
    opts = opts || {};
    if (!opts.force && checkedRecently()) return Promise.resolve("skipped");
    if (navigator.onLine === false) return Promise.resolve("failed");
    return load().then(function (rec) {
      var headers = { "Accept": "application/json" };
      if (rec && rec.etag && !isExpired(rec)) headers["If-None-Match"] = '"' + rec.etag + '"';
      return fetchWithTimeout(SNAPSHOT_URL, { credentials: "same-origin", headers: headers, cache: "no-store" })
        .then(function (resp) {
          if (resp.status === 401 || resp.status === 403) {
            return wipe().then(function () { return "denied"; });
          }
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
    MAX_AGE_DAYS: MAX_AGE_DAYS,
  };
})();
