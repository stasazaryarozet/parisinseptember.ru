/* Dela cookie banner — 152-ФЗ explicit-consent gate.
 *
 * Extracted from inline (site_generator._cookie_banner) for CSP-readiness:
 * `script-src 'self'` admits this file without `'unsafe-inline'`.
 *
 * Storage-key читается из data-атрибута [data-cookie-banner data-key="…"]
 * — single SoT остаётся spec.enforcement_data.Inv-COOKIE-banner.storage_key,
 * проброшенный шаблоном в HTML. Никаких inline-литералов в этом файле.
 *
 * Идемпотентен: если localStorage уже содержит решение — баннер не показывается.
 */
(function () {
  var b = document.querySelector("[data-cookie-banner]");
  if (!b) return;
  var K = b.getAttribute("data-key") || "";
  if (!K) return;
  try { if (localStorage.getItem(K)) { return; } } catch (e) { /* private mode */ }
  b.hidden = false;
  function set(v) {
    try { localStorage.setItem(K, JSON.stringify({ v: v, t: Date.now() })); } catch (e) { /* noop */ }
    b.hidden = true;
  }
  var ya = b.querySelector("[data-cookie-accept]");
  var yd = b.querySelector("[data-cookie-decline]");
  if (ya) ya.addEventListener("click", function () { set("all"); });
  if (yd) yd.addEventListener("click", function () { set("necessary"); });
})();
