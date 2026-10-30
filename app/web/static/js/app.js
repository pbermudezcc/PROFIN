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
    toast(status === 403 ? "Petición no permitida" : "No se pudo completar. Inténtalo de nuevo.", "err");
  });
  document.addEventListener("htmx:sendError", function () { toast("Sin conexión con el PC de casa", "err"); });

  // ── Toast ───────────────────────────────────────────────────────────
  var toastTimer;
  function toast(message, kind) {
    var el = $(".toast");
    if (!el) return;
    el.textContent = message;
    el.classList.toggle("err", kind === "err");
    el.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { el.classList.remove("show"); }, 2400);
  }
  document.addEventListener("toast", function (e) { toast(e.detail.value); });
  document.addEventListener("toastError", function (e) { toast(e.detail.value, "err"); });

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
  // iOS keeps the layout viewport full height when the keyboard opens, so a
  // bottom sheet would sit behind it: track the visible area instead.
  if (window.visualViewport) {
    var fitSheet = function () {
      var d = sheet(), vv = window.visualViewport;
      if (!d) return;
      var kb = Math.max(0, Math.round(window.innerHeight - vv.height - vv.offsetTop));
      d.style.setProperty("--kb", kb + "px");
      d.style.setProperty("--vvh", Math.round(vv.height) + "px");
    };
    window.visualViewport.addEventListener("resize", fitSheet);
    window.visualViewport.addEventListener("scroll", fitSheet);
    fitSheet();
  }
  document.addEventListener("refreshPage", function () {
    // Only the page content: the toast and the sheet live outside #main.
    if (window.htmx) htmx.ajax("GET", window.location.pathname + window.location.search,
                               { target: "#main", select: "#main", swap: "outerHTML" });
    else window.location.reload();
  });
  // ── Master–detail: on wide screens (iPad landscape and up) a page with a
  // #detail pane shows the item there instead of in the bottom sheet.
  var WIDE = window.matchMedia("(min-width: 1180px)");
  function pane() { var p = $("#detail"); return p && WIDE.matches ? p : null; }
  document.addEventListener("click", function (e) {
    var opener = e.target.closest && e.target.closest("[data-open-sheet][data-pane]");
    if (!opener) return;
    opener.setAttribute("hx-target", pane() ? "#detail" : "#sheet-body");
    if (pane()) {
      $$(".row.sel").forEach(function (r) { r.classList.remove("sel"); });
      opener.classList.add("sel");
      pane().setAttribute("data-url", opener.getAttribute("hx-get"));
    }
  }, true);
  function clearPane() {
    var p = $("#detail"); if (!p) return;
    p.removeAttribute("data-url");
    var tpl = $("#detail-empty");
    p.innerHTML = tpl ? tpl.innerHTML : "";
    $$(".row.sel").forEach(function (r) { r.classList.remove("sel"); });
  }
  // After a save from the pane, reload it so it shows the new values.
  function refreshPane() {
    var p = pane();
    if (p && p.getAttribute("data-url") && window.htmx) htmx.ajax("GET", p.getAttribute("data-url"), { target: "#detail" });
  }
  document.addEventListener("closeSheet", refreshPane);

  document.addEventListener("htmx:beforeRequest", function (e) {
    var trigger = e.detail.elt;
    if (trigger && trigger.hasAttribute && trigger.hasAttribute("data-open-sheet") && e.detail.target && e.detail.target.id !== "detail") {
      var body = $("#sheet-body");
      if (body) {
        body.innerHTML = '<div class="sheet-b"><div class="skel-row"><div class="skel a"></div><div class="skel b"></div></div>' +
                         '<div class="skel-row"><div class="skel b"></div><div class="skel c"></div></div></div>';
      }
      openSheet();
    }
  });
  var pressedOnBackdrop = false;
  document.addEventListener("pointerdown", function (e) { pressedOnBackdrop = e.target === sheet(); }, true);
  document.addEventListener("click", function (e) {
    var t = e.target;
    if (t.closest("[data-close-sheet]")) {
      if (t.closest("#detail")) clearPane(); else closeSheet();
      return;
    }
    // Tap on the backdrop — only when the press started there too: selecting
    // text in a field and releasing outside the sheet makes the browser report
    // the click on the <dialog> itself, which used to close the sheet.
    if (t === sheet()) { if (pressedOnBackdrop) closeSheet(); return; }

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

  // ── Confirmation dialog for hx-confirm (no blocking window.confirm) ──
  document.addEventListener("htmx:confirm", function (e) {
    var question = e.detail.question;
    if (!question) return;
    var dlg = $("#confirm");
    if (!dlg || !dlg.showModal) return;          // fall back to the browser dialog
    e.preventDefault();
    var elt = e.detail.elt;
    $("#confirm-title").textContent = question;
    $("[data-confirm-text]", dlg).textContent = elt.getAttribute("data-confirm-detail") || "Esta acción no se puede deshacer.";
    $("[data-confirm-ok]", dlg).textContent = elt.getAttribute("data-confirm-label") || "Confirmar";
    function done(ok) {
      $("[data-confirm-ok]", dlg).onclick = null;
      $("[data-confirm-cancel]", dlg).onclick = null;
      dlg.close();
      if (ok) e.detail.issueRequest(true);
    }
    $("[data-confirm-ok]", dlg).onclick = function () { done(true); };
    $("[data-confirm-cancel]", dlg).onclick = function () { done(false); };
    dlg.showModal();
  });

  // ── Sortable tables: <table data-sortable> + <th data-sort> ─────────
  // Cells may carry data-value (raw number/ISO date); otherwise the text is
  // read as a Spanish-formatted number when it looks like one.
  function cellKey(td) {
    if (!td) return "";
    var raw = td.hasAttribute("data-value") ? td.getAttribute("data-value") : td.textContent.trim();
    var t = raw.replace(/[\s€%]/g, "").replace(/−/g, "-");
    if (/^[-+]?[\d.]*,?\d+$/.test(t) && !td.hasAttribute("data-value")) t = t.replace(/\./g, "").replace(",", ".");
    var n = parseFloat(t);
    return (t !== "" && !isNaN(n) && /^[-+]?[\d.]+(e[-+]?\d+)?$/i.test(t)) ? n : raw.toLowerCase();
  }
  document.addEventListener("click", function (e) {
    var th = e.target.closest && e.target.closest("table[data-sortable] th[data-sort]");
    if (!th) return;
    var table = th.closest("table"), body = table.tBodies[0];
    var col = Array.prototype.indexOf.call(th.parentNode.children, th);
    var asc = th.getAttribute("aria-sort") !== "ascending";
    $$("th[aria-sort]", table).forEach(function (h) { h.removeAttribute("aria-sort"); });
    th.setAttribute("aria-sort", asc ? "ascending" : "descending");
    var rows = Array.prototype.slice.call(body.rows);
    rows.sort(function (a, b) {
      var x = cellKey(a.cells[col]), y = cellKey(b.cells[col]);
      if (x === "" && y !== "") return 1;
      if (y === "" && x !== "") return -1;
      var r = (typeof x === "number" && typeof y === "number") ? x - y : String(x).localeCompare(String(y), "es");
      return asc ? r : -r;
    });
    rows.forEach(function (r) { body.appendChild(r); });
  });

  // ── Statement import: live total + busy buttons ─────────────────────
  // Possible duplicates appear twice (phone list + tablet table): keep both
  // checkboxes of a row in sync and count each row once.
  document.addEventListener("change", function (e) {
    if (!e.target.matches("[data-review]")) return;
    var form = e.target.closest("[data-import-form]");
    if (!form) return;
    $$('[data-review][value="' + e.target.value + '"]', form).forEach(function (c) { c.checked = e.target.checked; });
    var chosen = {};
    $$("[data-review]:checked", form).forEach(function (c) { chosen[c.value] = true; });
    var total = parseInt(form.getAttribute("data-clean"), 10) + Object.keys(chosen).length;
    $$("[data-import-total]", form).forEach(function (el) { el.textContent = total; });
  });
  // ── «Nuevo usuario» sheet: live password checklist, Enter = next field,
  // a corrected field stops showing the server's error. ─────────────
  document.addEventListener("input", function (e) {
    var form = e.target.closest && e.target.closest("form[data-pw-check]");
    if (!form) return;
    var field = e.target.closest(".field.invalid");
    if (field && !e.target.hasAttribute("hx-get")) {
      field.classList.remove("invalid");
      var err = field.querySelector(".error"); if (err) err.remove();
    }
    var pw = form.querySelector("[data-pw]"), again = form.querySelector("[data-pw-confirm]");
    if (!pw || !again) return;                                     // name-only forms
    var min = parseInt(form.getAttribute("data-min"), 10) || 8;
    var max = parseInt(form.getAttribute("data-max"), 10) || 64;
    function rule(name, ok) {
      var li = form.querySelector('[data-rule="' + name + '"]');
      if (li) li.classList.toggle("ok", ok);
    }
    rule("length", pw.value.length >= min && pw.value.length <= max);
    rule("match", pw.value.length > 0 && pw.value === again.value);
  });
  // «Borrar mi cuenta»: the button wakes up only when the exact name and a
  // password are typed.
  document.addEventListener("input", function (e) {
    var form = e.target.closest && e.target.closest("form[data-delete-form]");
    if (!form) return;
    var name = form.querySelector('[name="confirm_name"]'), pw = form.querySelector('[name="password"]');
    var ok = name.value.trim() === form.getAttribute("data-username") && pw.value.length > 0;
    form.querySelector("[data-delete-submit]").disabled = !ok;
  });
  // ── Invitations: countdown, copy, share; code typing on /registro ──
  var inviteTimer;
  function initInvite(root) {
    var box = root.querySelector ? root.querySelector("[data-invite]") : null;
    if (!box) return;
    clearInterval(inviteTimer);
    var expires = parseInt(box.getAttribute("data-expires"), 10);
    var out = box.querySelector("[data-countdown]");
    function tick() {
      if (!document.body.contains(box)) { clearInterval(inviteTimer); return; }   // sheet closed
      var left = Math.max(0, Math.round((expires - Date.now()) / 1000));
      if (out) out.textContent = Math.floor(left / 60) + ":" + ("0" + (left % 60)).slice(-2);
      if (!left) {
        clearInterval(inviteTimer);
        box.classList.add("expired");
        var gone = box.querySelector(".invite-expired"); if (gone) gone.hidden = false;
        $$("[data-copy], [data-share]", box.parentElement).forEach(function (b) { b.disabled = true; });
      }
    }
    tick();
    inviteTimer = setInterval(tick, 1000);
    if (navigator.share) {
      $$("[data-share]", box.parentElement).forEach(function (b) { b.hidden = false; });
      $$("[data-share-fallback]", box.parentElement).forEach(function (b) { b.hidden = true; });
    }
  }
  document.addEventListener("htmx:afterSwap", function (e) { if (e.detail.target) initInvite(e.detail.target); });
  document.addEventListener("click", function (e) {
    var copy = e.target.closest && e.target.closest("[data-copy]");
    if (copy) {
      var text = copy.getAttribute("data-copy");
      var done = function () { toast("Código copiado"); };
      if (navigator.clipboard && window.isSecureContext) {
        navigator.clipboard.writeText(text).then(done, function () { toast("No se pudo copiar", "err"); });
      } else {
        var range = document.createRange(), el = $("[data-invite-text]");
        if (el) { range.selectNodeContents(el); var sel = getSelection(); sel.removeAllRanges(); sel.addRange(range); }
        toast("Mantén pulsado el código para copiarlo");
      }
      return;
    }
    var share = e.target.closest && e.target.closest("[data-share]");
    if (share && navigator.share) {
      var code = share.getAttribute("data-share");
      navigator.share({
        title: "Invitación a FAMILYFIN",
        text: "Tu código para entrar en FAMILYFIN: " + code + " (un solo uso, caduca en 15 minutos). " +
              "Ábrelo en " + location.origin + "/registro",
      }).catch(function () {});
    }
  });
  document.addEventListener("input", function (e) {
    if (!e.target.matches || !e.target.matches("[data-invite-code]")) return;
    var raw = e.target.value.toUpperCase().replace(/[^A-Z0-9]/g, "").slice(0, 8);
    var pretty = raw.length > 4 ? raw.slice(0, 4) + "-" + raw.slice(4) : raw;
    if (pretty !== e.target.value) e.target.value = pretty;
  });

  // A password change rotates the session: keep the page's CSRF token current.
  document.addEventListener("csrfToken", function (e) {
    var token = e.detail.value;
    if (!token) return;
    var meta = $('meta[name="csrf-token"]'); if (meta) meta.content = token;
    $$('input[name="csrf_token"]').forEach(function (i) { i.value = token; });
  });
  document.addEventListener("keydown", function (e) {
    if (e.key !== "Enter" || !e.target.closest || !e.target.closest("form[data-pw-check]")) return;
    if (e.target.hasAttribute("data-pw-confirm")) return;          // last field: submit
    var inputs = $$('input:not([type="hidden"])', e.target.form);
    var next = inputs[inputs.indexOf(e.target) + 1];
    if (next) { e.preventDefault(); next.focus(); }
  });
  // The live name hint decides whether the name field looks invalid.
  document.addEventListener("htmx:afterSwap", function (e) {
    var slot = e.detail.target;
    if (!slot || !slot.hasAttribute || !slot.hasAttribute("data-field-hint")) return;
    var field = slot.closest(".field");
    if (field) field.classList.toggle("invalid", !!slot.querySelector(".error"));
  });
  document.addEventListener("submit", function (e) {
    var button = e.submitter;
    if (!button || !button.hasAttribute("data-busy-label") || e.defaultPrevented) return;
    // After the browser has collected the form data: disable to avoid a double import.
    setTimeout(function () { button.disabled = true; button.textContent = button.getAttribute("data-busy-label"); }, 0);
  });
  // Statement upload: the button says how many files were chosen.
  document.addEventListener("change", function (e) {
    var input = e.target;
    if (!input.hasAttribute || !input.hasAttribute("data-statement-files")) return;
    var label = input.form && input.form.querySelector("[data-upload-label]");
    if (!label) return;
    var n = input.files ? input.files.length : 0;
    label.textContent = n > 1 ? "Subir y revisar " + n + " extractos" : "Subir y revisar";
  });

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
    var fresh = e.detail.elt === document.body || e.detail.elt.id === "main";
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
