// LOG_ON dashboard: small progressive enhancements. Every page works without this file.
(function () {
  "use strict";
  // Audit filter: text + node select over table rows (data-text / data-node attributes).
  var q = document.getElementById("audit-q"), sel = document.getElementById("audit-node"), cnt = document.getElementById("audit-count");
  function filter() {
    var t = (q && q.value || "").toLowerCase(), n = sel && sel.value || "", shown = 0;
    document.querySelectorAll("#audit-rows tr[data-text]").forEach(function (tr) {
      var ok = (!t || tr.getAttribute("data-text").indexOf(t) >= 0) && (!n || tr.getAttribute("data-node") === n);
      tr.hidden = !ok; if (ok) shown++;
    });
    if (cnt) cnt.textContent = shown + " shown";
  }
  if (q) q.addEventListener("input", filter);
  if (sel) sel.addEventListener("change", filter);
  // Journey: j / k (or arrow keys while the run list has focus) move between runs.
  var runs = Array.prototype.slice.call(document.querySelectorAll(".runlist a"));
  if (runs.length) {
    var curEl = document.querySelector('.runlist a[aria-current="true"]'), list = document.querySelector(".runlist");
    if (curEl && list) list.scrollTop = Math.max(0, curEl.offsetTop - list.offsetTop - 80);
    document.addEventListener("keydown", function (e) {
      if (e.target.matches("input,textarea,select") || e.metaKey || e.ctrlKey || e.altKey) return;
      var inList = document.activeElement && document.activeElement.closest(".runlist");
      var cur = runs.findIndex(function (a) { return a.getAttribute("aria-current") === "true"; });
      var focus = runs.indexOf(document.activeElement);
      var i = focus >= 0 ? focus : cur;
      if (e.key === "j" || (inList && e.key === "ArrowDown")) { e.preventDefault(); runs[Math.min(runs.length - 1, i + 1)].focus(); }
      else if (e.key === "k" || (inList && e.key === "ArrowUp")) { e.preventDefault(); runs[Math.max(0, i - 1)].focus(); }
    });
  }
  // Approve/reject: confirm before a reject without a note (server enforces too).
  document.querySelectorAll("form.decide").forEach(function (f) {
    f.addEventListener("submit", function (e) {
      var btn = e.submitter, note = f.querySelector("textarea");
      if (btn && btn.value === "reject" && note && !note.value.trim()) { e.preventDefault(); note.focus(); note.setAttribute("aria-invalid", "true"); }
    });
  });
})();
