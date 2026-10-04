/* Applies the per-device theme choice before first paint (no flash). */
(function () {
  try {
    var t = localStorage.getItem("ff-theme");
    if (t === "light" || t === "dark") document.documentElement.setAttribute("data-theme", t);
  } catch (e) { /* private mode: follow the device */ }
})();
