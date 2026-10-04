/* FAMILYFIN web behaviour. No inline scripts anywhere (strict CSP):
   everything is wired here through data-* attributes and htmx events. */
(function () {
  "use strict";

  function $(sel, root) { return (root || document).querySelector(sel); }
  function $$(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }
  function csrf() { var m = $('meta[name="csrf-token"]'); return m ? m.content : ""; }

  // ── CSRF on every htmx request ──────────────────────────────────────
  document.addEventListener("htmx:configRequest", function (e) {
    var token = csrf();
    if (token) e.detail.headers["X-CSRF-Token"] = token;
  });

  // Session expired during an htmx call → back to the login page.
  document.addEventListener("htmx:responseError", function (e) {
    var status = e.detail.xhr.status;
    if (status === 401) { window.location.href = "/login"; return; }
    toast(status === 403 ? "Petición no permitida" : "No se pudo completar. Inténtalo de nuevo.");
  });
  document.addEventListener("htmx:sendError", function () { toast("Sin conexión con el PC de casa"); });

  // ── Toast ───────────────────────────────────────────────────────────
  var toastTimer;
  function toast(message) {
    var el = $(".toast");
    if (!el) return;
    el.textContent = message;
    el.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { el.classList.remove("show"); }, 2400);
  }
  document.addEventListener("toast", function (e) { toast(e.detail.value); });

  // ── Bottom sheet ────────────────────────────────────────────────────
  function sheet() { return $("#sheet"); }
  function openSheet() {
    var d = sheet();
    if (d && !d.open) { d.showModal(); document.documentElement.style.overflow = "hidden"; }
  }
  function closeSheet() {
    var d = sheet();
    if (d && d.open) d.close();
  }
  document.addEventListener("close", function (e) {
    if (e.target === sheet()) {
      document.documentElement.style.overflow = "";
      var body = $("#sheet-body"); if (body) body.textContent = "";
    }
  }, true);
  document.addEventListener("closeSheet", closeSheet);
  document.addEventListener("refreshPage", function () {
    if (window.htmx) htmx.ajax("GET", window.location.pathname + window.location.search,
                               { target: "body", swap: "innerHTML" });
    else window.location.reload();
  });
  document.addEventListener("htmx:beforeRequest", function (e) {
    var trigger = e.detail.elt;
    if (trigger && trigger.hasAttribute && trigger.hasAttribute("data-open-sheet")) {
      var body = $("#sheet-body");
      if (body) {
        body.innerHTML = '<div class="sheet-b"><div class="skel-row"><div class="skel a"></div><div class="skel b"></div></div>' +
                         '<div class="skel-row"><div class="skel b"></div><div class="skel c"></div></div></div>';
      }
      openSheet();
    }
  });
  document.addEventListener("click", function (e) {
    var t = e.target;
    if (t.closest("[data-close-sheet]")) { closeSheet(); return; }
    if (t === sheet()) { closeSheet(); return; }   // tap on the backdrop

    var toggle = t.closest("[data-toggle]");
    if (toggle) {
      e.preventDefault();
      var target = $(toggle.getAttribute("data-toggle"));
      if (target) target.hidden = !target.hidden;
      return;
    }
    var pw = t.closest("[data-toggle-password]");
    if (pw) {
      var input = pw.parentElement.querySelector("input");
      input.type = input.type === "password" ? "text" : "password";
      return;
    }
    if (t.closest("[data-theme-toggle]")) {
      var dark = document.documentElement.getAttribute("data-theme") === "dark" ||
        (!document.documentElement.hasAttribute("data-theme") && matchMedia("(prefers-color-scheme: dark)").matches);
      setTheme(dark ? "light" : "dark");
      return;
    }
    var set = t.closest("[data-theme-set]");
    if (set) { setTheme(set.getAttribute("data-theme-set")); }
  });

  // Swipe the sheet down to close it (phones).
  (function () {
    var startY = null, d;
    document.addEventListener("touchstart", function (e) {
      d = sheet();
      if (!d || !d.open || !e.target.closest(".grab, .sheet-h")) { startY = null; return; }
      startY = e.touches[0].clientY;
    }, { passive: true });
    document.addEventListener("touchmove", function (e) {
      if (startY === null) return;
      var dy = Math.max(0, e.touches[0].clientY - startY);
      d.style.transform = "translateY(" + dy + "px)";
    }, { passive: true });
    document.addEventListener("touchend", function (e) {
      if (startY === null) return;
      var dy = e.changedTouches[0].clientY - startY;
      d.style.transform = "";
      if (dy > 90) closeSheet();
      startY = null;
    });
  })();

  // ── Theme ───────────────────────────────────────────────────────────
  function setTheme(mode) {
    try {
      if (mode === "auto") localStorage.removeItem("ff-theme"); else localStorage.setItem("ff-theme", mode);
    } catch (e) { /* ignore */ }
    if (mode === "auto") document.documentElement.removeAttribute("data-theme");
    else document.documentElement.setAttribute("data-theme", mode);
    markThemeSeg();
    if (window.FFCharts) FFCharts.retheme();
  }
  function markThemeSeg() {
    var current = document.documentElement.getAttribute("data-theme") || "auto";
    $$("[data-theme-set]").forEach(function (b) {
      b.classList.toggle("on", b.getAttribute("data-theme-set") === current);
      b.setAttribute("aria-checked", b.getAttribute("data-theme-set") === current ? "true" : "false");
    });
  }

  // ── Category search inside the transaction sheet ────────────────────
  document.addEventListener("input", function (e) {
    if (!e.target.matches("[data-cat-search]")) return;
    var q = e.target.value.trim().toLowerCase()
      .normalize("NFD").replace(/[̀-ͯ]/g, "");
    var body = e.target.closest(".sheet-b") || document;
    var any = false;
    $$(".cat-group", body).forEach(function (group) {
      var visible = 0;
      $$("label[data-cat-name]", group).forEach(function (label) {
        var name = label.getAttribute("data-cat-name").normalize("NFD").replace(/[̀-ͯ]/g, "");
        var show = !q || name.indexOf(q) >= 0;
        label.hidden = !show;
        if (show) visible++;
      });
      group.hidden = visible === 0;
      if (visible) any = true;
    });
    var empty = $("[data-cat-empty]", body);
    if (empty) empty.hidden = any;
  });

  // ── Auto-submit selects ─────────────────────────────────────────────
  document.addEventListener("change", function (e) {
    if (e.target.matches("select[data-autosubmit]")) {
      var form = e.target.form;
      if (form.requestSubmit) form.requestSubmit(); else form.submit();
    }
  });

  // ── Investment operation form ───────────────────────────────────────
  function initOpForm(root) {
    $$("form[data-op-form]", root).forEach(function (form) {
      if (form.__ff) return; form.__ff = true;
      var labels = { buy: "Importe total cargado (EUR)", sell: "Importe neto recibido (EUR)",
                     dividend: "Dividendo neto (EUR)", grant: "Valor fiscal al recibirlas (EUR)" };
      function kind() { var k = form.querySelector('input[name="kind"]:checked'); return k ? k.value : "buy"; }
      function security() { return form.querySelector('select[name="security_id"]'); }
      function currency() {
        var opt = security().selectedOptions[0];
        return (opt && opt.getAttribute("data-currency")) || "EUR";
      }
      function num(name) {
        var el = form.querySelector('[name="' + name + '"]');
        if (!el || !el.value.trim()) return null;
        var t = el.value.trim().replace(/\s|€/g, "");
        if (t.indexOf(",") >= 0) t = t.replace(/\./g, "").replace(",", ".");
        var v = parseFloat(t); return isNaN(v) ? null : v;
      }
      function sync() {
        var k = kind();
        $$("[data-show-for]", form).forEach(function (el) {
          var show = el.getAttribute("data-show-for").split(" ").indexOf(k) >= 0;
          el.hidden = !show;
          $$("input, select, textarea", el).forEach(function (i) { i.disabled = !show; });
        });
        var amount = form.querySelector('input[name="amount_eur"]');
        if (amount) amount.required = k !== "split" && k !== "grant";
        var lbl = form.querySelector("[data-amount-label]"); if (lbl && labels[k]) lbl.textContent = labels[k];
        var cur = currency();
        form.querySelector('input[name="currency"]').value = cur;
        var cl = form.querySelector("[data-currency-label]"); if (cl) cl.textContent = cur;
        hint();
      }
      function hint() {
        var box = form.querySelector("[data-calc-hint]"); if (!box) return;
        var k = kind(), q = num("quantity"), p = num("price"), f = num("fees_eur") || 0;
        if ((k === "buy" || k === "sell") && q && p) {
          var gross = q * p;
          if (currency() === "EUR") {
            var total = k === "sell" ? gross - f : gross + f;
            box.textContent = "Sugerido: " + total.toLocaleString("es-ES", { style: "currency", currency: "EUR" }) + " — toca para usarlo.";
            box.setAttribute("data-value", total.toFixed(2));
          } else {
            box.textContent = "Bruto: " + gross.toLocaleString("es-ES", { maximumFractionDigits: 2 }) + " " + currency() + ". Indica el importe en EUR que aparece en el bróker.";
            box.removeAttribute("data-value");
          }
          box.hidden = false;
        } else { box.hidden = true; }
      }
      form.addEventListener("change", sync);
      form.addEventListener("input", function (e) { if (e.target.hasAttribute("data-calc")) hint(); });
      var box = form.querySelector("[data-calc-hint]");
      if (box) box.addEventListener("click", function () {
        var v = box.getAttribute("data-value");
        if (v) form.querySelector('input[name="amount_eur"]').value = v.replace(".", ",");
      });
      sync();
    });
  }

  // ── Topbar shadow + small title once the large title scrolls away ───
  function onScroll() {
    var bar = $("[data-topbar]");
    if (bar) bar.classList.toggle("scrolled", window.scrollY > 40);
  }
  window.addEventListener("scroll", onScroll, { passive: true });

  // ── Pull to refresh (home-screen app has no reload button) ──────────
  (function () {
    var startY = null, pulled = 0, ptr;
    document.addEventListener("touchstart", function (e) {
      if (window.scrollY > 0 || (sheet() && sheet().open)) { startY = null; return; }
      startY = e.touches[0].clientY; pulled = 0; ptr = $(".ptr");
    }, { passive: true });
    document.addEventListener("touchmove", function (e) {
      if (startY === null || !ptr) return;
      pulled = Math.max(0, e.touches[0].clientY - startY);
      var y = Math.min(90, pulled / 2) - 80;
      ptr.style.transform = "translate(-50%," + y + "px) rotate(" + pulled * 2 + "deg)";
    }, { passive: true });
    document.addEventListener("touchend", function () {
      if (startY === null || !ptr) return;
      if (pulled > 110) {
        ptr.classList.add("go");
        window.location.reload();
      } else { ptr.style.transform = ""; }
      startY = null;
    });
  })();

  // ── Page / fragment initialisation ──────────────────────────────────
  function initAll(root, fresh) {
    if (window.FFCharts) FFCharts.init(root, fresh);
    initOpForm(root);
    markThemeSeg();
    onScroll();
  }
  document.addEventListener("htmx:load", function (e) {
    var fresh = e.detail.elt === document.body;
    initAll(e.detail.elt, fresh);
  });
  document.addEventListener("htmx:beforeSwap", function (e) {
    if (e.detail.target === document.body) closeSheet();
  });
  document.addEventListener("htmx:historyRestore", function () { initAll(document, true); });
  document.addEventListener("DOMContentLoaded", function () {
    if (!window.htmx) initAll(document, true);   // htmx fires htmx:load on body itself
  });

  // Progress bar while htmx works.
  document.addEventListener("htmx:beforeRequest", function () { document.body.classList.add("loading"); });
  document.addEventListener("htmx:afterRequest", function () { document.body.classList.remove("loading"); });

  // ── Service worker (static shell only, never financial data) ────────
  if ("serviceWorker" in navigator && window.isSecureContext) {
    window.addEventListener("load", function () {
      navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch(function () {});
    });
  }
})();
