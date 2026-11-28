/*
 * Схема территории на оффлайн-странице (static/offline/app.js) — просмотр
 * без правки. Рисует то же, что garage_map/view.html: подложку, боксы с
 * окраской, номера, проезды и здания, линии с фазами, оборудование и связи
 * сети; размеры и цвета — те же (стили gm-* в offline/app.html).
 *
 * window.offlineMap.render(container, map, ctx):
 *   map — data.map слепка (app/offline.py: _map_snapshot)
 *   ctx.state(garageId) → {territory, debt, power, land, owners} | null
 *   ctx.number(garageId) → номер гаража
 *   ctx.focus — {kind: "g"|"o", id} — выделить и показать
 * Щелчок по боксу — #g/<id>, по оборудованию — #o/<id>.
 */
(function () {
  "use strict";
  var NS = "http://www.w3.org/2000/svg";
  var GLYPH = { panel: "", lamp: "", meter: "", "switch": "", wifi: "", netswitch: "", dvr: "", camera: "", mark: "" };
  var WIFI_GLYPH = { router: "", bridge: "" };
  var KINDS = { panel: "Электрощит", lamp: "Фонарь", meter: "Счётчик", "switch": "Рубильник", wifi: "Wi-Fi",
    netswitch: "Коммутатор", dvr: "Видеорегистратор", camera: "Камера", mark: "Отметка" };
  var MODES = {
    territory: ["inside", "outside", "none"], debt: ["debt", "ok", "none"],
    power: ["fresh", "stale", "nometer", "none"], land: ["privatized", "notprivatized", "none"]
  };
  var MODE_NAMES = { territory: "Территория", debt: "Долг", power: "Показания", land: "Земля" };
  var STATE_NAMES = {
    inside: "в кооперативе", outside: "за территорией", none: "не привязан к реестру", debt: "есть долг", ok: "без долга",
    fresh: "показания свежие", stale: "показания давно не подавались", nometer: "нет счётчика",
    privatized: "земля приватизирована", notprivatized: "земля не приватизирована"
  };
  var LAYERS = [
    ["garages", "Боксы"], ["labels", "Номера"], ["roads", "Проезды, здания"], ["cables", "Линии и фазы"],
    ["power", "Электрика"], ["lamps", "Фонари"], ["network", "Сеть"], ["video", "Видео"], ["links", "Связи сети"], ["marks", "Отметки"]
  ];
  var OBJ_LAYER = { panel: "power", meter: "power", "switch": "power", lamp: "lamps", wifi: "network", netswitch: "network",
    dvr: "video", camera: "video", mark: "marks" };
  var SHAPE_LAYER = { garage: "garages", road: "roads", building: "roads", area: "roads", cable: "cables" };

  var store = {
    get: function (k, d) { try { var v = localStorage.getItem(k); return v === null ? d : v; } catch (e) { return d; } },
    set: function (k, v) { try { localStorage.setItem(k, v); } catch (e) {} }
  };
  var scrollPos = null;  // прокрутка схемы — восстанавливается после возврата из карточки

  function el(name, attrs, parent) {
    var e = document.createElementNS(NS, name);
    for (var k in attrs) e.setAttribute(k, attrs[k]);
    if (parent) parent.appendChild(e);
    return e;
  }
  function centroid(pts) {
    var x = 0, y = 0;
    pts.forEach(function (p) { x += p[0]; y += p[1]; });
    return [x / pts.length, y / pts.length];
  }
  function dims(pts) {
    var xs = pts.map(function (p) { return p[0]; }), ys = pts.map(function (p) { return p[1]; });
    return [Math.max.apply(null, xs) - Math.min.apply(null, xs), Math.max.apply(null, ys) - Math.min.apply(null, ys)];
  }
  function offsetPolyline(pts, d) {
    return pts.map(function (p, i) {
      var nx = 0, ny = 0;
      [[pts[i - 1], p], [p, pts[i + 1]]].forEach(function (seg) {
        if (!seg[0] || !seg[1]) return;
        var dx = seg[1][0] - seg[0][0], dy = seg[1][1] - seg[0][1], len = Math.hypot(dx, dy) || 1;
        nx += -dy / len; ny += dx / len;
      });
      var nl = Math.hypot(nx, ny) || 1;
      return [p[0] + nx / nl * d, p[1] + ny / nl * d];
    });
  }
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function render(container, map, ctx) {
    var W = map.width, H = map.height;
    var rotated = store.get("olMapRotated", "0") === "1";
    var zoom = parseFloat(store.get("olMapZoom", "1")) || 1;
    var mode = store.get("olMapMode", "territory");
    if (!MODES[mode]) mode = "territory";
    var layers = {};
    LAYERS.forEach(function (l) { layers[l[0]] = true; });
    try {
      var saved = JSON.parse(store.get("olMapLayers", "null"));
      if (saved) for (var k in saved) if (k in layers) layers[k] = !!saved[k];
    } catch (e) {}
    var objectById = {};
    map.objects.forEach(function (o) { objectById[o.id] = o; });
    var focus = ctx.focus || null;

    container.innerHTML =
      '<div class="d-flex flex-wrap gap-2 align-items-center mb-2">' +
      '<input class="form-control form-control-sm" style="width: 7rem" id="gmSearch" placeholder="№ гаража" autocomplete="off" inputmode="search">' +
      '<select class="form-select form-select-sm w-auto" id="gmMode" aria-label="Окраска">' +
      Object.keys(MODES).map(function (m) { return '<option value="' + m + '"' + (m === mode ? " selected" : "") + ">" + MODE_NAMES[m] + "</option>"; }).join("") +
      "</select>" +
      '<div class="btn-group btn-group-sm" role="group">' +
      '<button type="button" class="btn btn-outline-secondary" id="gmZoomOut" title="Мельче" aria-label="Мельче"><i class="bi bi-zoom-out"></i></button>' +
      '<button type="button" class="btn btn-outline-secondary" id="gmZoomIn" title="Крупнее" aria-label="Крупнее"><i class="bi bi-zoom-in"></i></button>' +
      '<button type="button" class="btn btn-outline-secondary" id="gmRotate" title="Повернуть" aria-label="Повернуть"><i class="bi bi-arrow-repeat"></i></button>' +
      "</div></div>" +
      '<details class="small mb-2"><summary>Слои</summary><div id="gmLayers" class="d-flex flex-wrap gap-3 pt-2"></div></details>' +
      '<div id="gmLegend" class="small d-flex flex-wrap gap-3 mb-2"></div>' +
      '<div id="gmWrap"><svg id="gmSvg" xmlns="http://www.w3.org/2000/svg"></svg></div>';
    var svg = container.querySelector("#gmSvg");
    var wrap = container.querySelector("#gmWrap");
    var search = container.querySelector("#gmSearch");

    function layerOn(name) { return layers[name] !== false; }
    function view(p) { return rotated ? [p[1], W - p[0]] : [p[0], p[1]]; }
    function vw() { return rotated ? H : W; }
    function vh() { return rotated ? W : H; }
    function stateOf(shape) {
      var st = shape.garage_id && ctx.state(shape.garage_id);
      return st ? st[mode] : "none";
    }
    function labelOf(shape) {
      return (shape.garage_id && ctx.number(shape.garage_id)) || shape.label || "";
    }
    function isFocus(item, kind) {
      if (!focus || focus.kind !== kind) return false;
      return kind === "g" ? item.garage_id === focus.id : item.id === focus.id;
    }
    function frameBox() {
      var full = [0, 0, vw(), vh()];
      var x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
      function add(p) { x0 = Math.min(x0, p[0]); y0 = Math.min(y0, p[1]); x1 = Math.max(x1, p[0]); y1 = Math.max(y1, p[1]); }
      map.shapes.forEach(function (s) { if (s.kind !== "road") s.points.forEach(function (p) { add(view(p)); }); });
      map.objects.forEach(function (o) { add(view([o.x, o.y])); });
      if (x0 === Infinity) return full;
      var pad = Math.max(x1 - x0, y1 - y0) * 0.03;
      x0 = Math.max(0, x0 - pad); y0 = Math.max(0, y0 - pad);
      x1 = Math.min(vw(), x1 + pad); y1 = Math.min(vh(), y1 + pad);
      return x1 > x0 && y1 > y0 ? [x0, y0, x1 - x0, y1 - y0] : full;
    }

    function renderLayers() {
      var box = container.querySelector("#gmLayers");
      box.innerHTML = LAYERS.map(function (l) {
        return '<label class="form-check mb-0"><input class="form-check-input" type="checkbox" data-layer="' + l[0] + '"' +
          (layerOn(l[0]) ? " checked" : "") + '> <span class="form-check-label">' + l[1] + "</span></label>";
      }).join("");
    }

    function renderLegend() {
      var counts = {};
      map.shapes.forEach(function (s) { if (s.kind === "garage") { var k = stateOf(s); counts[k] = (counts[k] || 0) + 1; } });
      var html = MODES[mode].filter(function (k) { return counts[k] || k !== "none"; }).map(function (k) {
        return '<span><span class="gm-legend-swatch gm-sw-' + k + '"></span> ' + STATE_NAMES[k] + " (" + (counts[k] || 0) + ")</span>";
      }).join("");
      if (layerOn("cables") && map.shapes.some(function (s) { return s.kind === "cable"; })) {
        html += ["A", "B", "C"].map(function (ph) {
          return '<span><span class="gm-phase-swatch gm-ph-' + ph + '"></span> Фаза ' + ph + "</span>";
        }).join("");
      }
      container.querySelector("#gmLegend").innerHTML = html;
    }

    function draw() {
      svg.innerHTML = "";
      renderLegend();
      var frame = frameBox();
      svg.setAttribute("viewBox", frame.map(function (v) { return Math.round(v); }).join(" "));
      var natural = Math.max(wrap.clientWidth - 2, 280);
      svg.setAttribute("width", Math.round(natural * zoom));
      svg.setAttribute("height", Math.round(natural * zoom * frame[3] / frame[2]));
      if (map.background) {
        var bg = el("g", { opacity: (map.background_opacity == null ? 50 : map.background_opacity) / 100 }, svg);
        var img = el("image", { href: map.background, x: 0, y: 0, width: W, height: H, preserveAspectRatio: "none" }, bg);
        if (rotated) img.setAttribute("transform", "translate(0 " + W + ") rotate(-90)");
      }
      var order = { area: 0, road: 1, building: 2, garage: 3, cable: 4 };
      var phaseGap = 3.5 * frame[2] / (parseFloat(svg.getAttribute("width")) || frame[2]);
      var widths = map.shapes.filter(function (s) { return s.kind === "garage"; })
        .map(function (s) { var d = dims(s.points.map(view)); return Math.min(d[0], d[1]); }).sort(function (a, b) { return a - b; });
      var common = widths.length ? widths[Math.floor(widths.length / 2)] / 2.1 : 20;
      var labels = [];
      map.shapes.slice().sort(function (a, b) { return order[a.kind] - order[b.kind]; }).forEach(function (shape) {
        var pts = shape.points.map(view);
        if (pts.length < 2) return;
        var focused = isFocus(shape, "g");
        if (!layerOn(SHAPE_LAYER[shape.kind]) && !focused) return;
        if (shape.kind === "cable") {
          var phases = (shape.phases || "").split("").filter(function (p) { return "ABC".indexOf(p) !== -1; });
          var lines = phases.length ? phases : ["none"];
          lines.forEach(function (ph, i) {
            var shift = (i - (lines.length - 1) / 2) * phaseGap;
            var off = shift ? offsetPolyline(pts, shift) : pts;
            el("polyline", { points: off.map(function (p) { return p.join(","); }).join(" "), "class": "gm-cable gm-cable-" + ph }, svg);
          });
          return;
        }
        var cls = "gm-shape gm-" + shape.kind;
        if (shape.kind === "garage") cls += " s-" + stateOf(shape);
        if (focused) cls += " gm-selected";
        var poly = el("polygon", { points: pts.map(function (p) { return p.join(","); }).join(" "), "class": cls }, svg);
        poly.__shape = shape;
        if (shape.kind === "garage") {
          el("line", { x1: pts[0][0], y1: pts[0][1], x2: pts[1][0], y2: pts[1][1], "class": "gm-gate" }, svg);
          var st = shape.garage_id && ctx.state(shape.garage_id);
          el("title", {}, poly).textContent = labelOf(shape) + (st && st.owners ? " — " + st.owners : "");
        }
        var text = labelOf(shape);
        if (text && layerOn("labels")) labels.push([shape, pts, text]);
      });
      labels.forEach(function (item) {
        var shape = item[0], pts = item[1], text = item[2], c = centroid(pts), d = dims(pts);
        var upright = shape.kind !== "garage" && d[1] > d[0] * 1.5;
        var along = upright ? d[1] : d[0], across = upright ? d[0] : d[1];
        var size = shape.kind === "garage"
          ? Math.min(common, d[0] / (text.length * 0.62 + 0.3), d[1] * 0.7)
          : Math.min(across * 0.45, along * 0.9 / (text.length * 0.6), common * 1.5);
        var t = el("text", { x: c[0], y: c[1], "font-size": Math.max(size, 6).toFixed(1),
          "class": "gm-label" + (shape.garage_id || shape.kind !== "garage" ? "" : " gm-label-muted") }, svg);
        if (upright) t.setAttribute("transform", "rotate(-90 " + c[0] + " " + c[1] + ")");
        t.textContent = text;
      });
      var R = Math.max(common * 0.5, 8);
      function objShown(o) { return isFocus(o, "o") || layerOn(OBJ_LAYER[o.kind] || "marks"); }
      if (layerOn("links")) map.objects.forEach(function (obj) {
        var up = obj.uplink_id && objectById[obj.uplink_id];
        if (!up || !objShown(obj) || !objShown(up)) return;
        var a = view([obj.x, obj.y]), b = view([up.x, up.y]);
        el("line", { x1: a[0], y1: a[1], x2: b[0], y2: b[1], "class": "gm-link" }, svg);
      });
      map.objects.forEach(function (obj) {
        if (!objShown(obj)) return;
        var v = view([obj.x, obj.y]);
        var grp = el("g", { "class": "gm-obj gm-obj-" + obj.kind + (isFocus(obj, "o") ? " gm-selected" : "") }, svg);
        el("circle", { cx: v[0], cy: v[1], r: R }, grp);
        el("text", { x: v[0], y: v[1], "font-size": (R * 1.15).toFixed(1) }, grp).textContent =
          (obj.kind === "wifi" && WIFI_GLYPH[obj.net_role]) || GLYPH[obj.kind] || GLYPH.mark;
        el("title", {}, grp).textContent = (KINDS[obj.kind] || "") + (obj.title ? ": " + obj.title : "");
        grp.__obj = obj;
      });
      applySearch(false);
    }

    function hitElements() {
      var q = search.value.trim().toLowerCase();
      var hits = [];
      svg.querySelectorAll("polygon.gm-garage").forEach(function (poly) {
        var on = !!q && String(labelOf(poly.__shape)).toLowerCase() === q;
        poly.classList.toggle("gm-hit", on);
        if (on) hits.push(poly);
      });
      return hits;
    }
    function centerOn(node) {
      var r = node.getBoundingClientRect(), w = wrap.getBoundingClientRect();
      wrap.scrollLeft += r.left + r.width / 2 - (w.left + w.width / 2);
      wrap.scrollTop += r.top + r.height / 2 - (w.top + w.height / 2);
    }
    function applySearch(scroll) {
      var hits = hitElements();
      if (scroll && hits[0]) centerOn(hits[0]);
    }

    function redraw(keepCenter) {
      var cx = 0.5, cy = 0.5;
      if (keepCenter && wrap.scrollWidth) {
        cx = (wrap.scrollLeft + wrap.clientWidth / 2) / wrap.scrollWidth;
        cy = (wrap.scrollTop + wrap.clientHeight / 2) / wrap.scrollHeight;
      }
      draw();
      if (keepCenter) {
        wrap.scrollLeft = cx * wrap.scrollWidth - wrap.clientWidth / 2;
        wrap.scrollTop = cy * wrap.scrollHeight - wrap.clientHeight / 2;
      }
    }

    renderLayers();
    draw();
    var focusEl = svg.querySelector(".gm-selected");
    if (focusEl) {
      centerOn(focusEl);
    } else if (scrollPos) {
      wrap.scrollLeft = scrollPos[0]; wrap.scrollTop = scrollPos[1];
    }

    wrap.addEventListener("scroll", function () { scrollPos = [wrap.scrollLeft, wrap.scrollTop]; });
    svg.addEventListener("click", function (evt) {
      var t = evt.target;
      var grp = t.closest && t.closest(".gm-obj");
      if (grp && grp.__obj) { location.hash = "o/" + grp.__obj.id; return; }
      if (t.__shape && t.__shape.kind === "garage" && t.__shape.garage_id) location.hash = "g/" + t.__shape.garage_id;
    });
    search.addEventListener("input", function () { applySearch(true); });
    container.querySelector("#gmMode").addEventListener("change", function () {
      mode = this.value; store.set("olMapMode", mode); draw();
    });
    container.querySelector("#gmZoomIn").addEventListener("click", function () {
      zoom = Math.min(zoom * 1.5, 12); store.set("olMapZoom", String(zoom)); redraw(true);
    });
    container.querySelector("#gmZoomOut").addEventListener("click", function () {
      zoom = Math.max(zoom / 1.5, 1); store.set("olMapZoom", String(zoom)); redraw(true);
    });
    container.querySelector("#gmRotate").addEventListener("click", function () {
      rotated = !rotated; store.set("olMapRotated", rotated ? "1" : "0"); scrollPos = null; draw();
    });
    container.querySelector("#gmLayers").addEventListener("change", function (evt) {
      var name = evt.target.getAttribute("data-layer");
      if (!name) return;
      layers[name] = evt.target.checked;
      store.set("olMapLayers", JSON.stringify(layers));
      draw();
    });
  }

  window.offlineMap = { render: render, KINDS: KINDS, esc: esc };
})();
