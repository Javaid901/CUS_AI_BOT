/**
 * admin_sync_documents.js â€” Phase 1 "Sync Documents" review queue.
 *
 * Super-Admin surface for the website-sync document intelligence layer:
 * review classifications (knowledge / official / ambiguous), confirm the
 * metadata/trust state of preserved raw documents, or hold them out.
 *
 * Lifecycle: draft -> pending_review -> verified | hidden_hold
 * Verify confirms the document classification â€” it does NOT publish anything.
 * Model papers are classification-only and always held for review.
 *
 * Backs the admin "Sync Documents" tab (window.CUS.syncDocumentsInit).
 * Mirrors the admin_notices.js module conventions (self-contained IIFE).
 */
(function () {
  "use strict";

  if (!window.CUS_API_BASE) throw new Error("CUS_API_BASE not defined");
  var API = window.CUS_API_BASE;
  var BASE = API + "/api/admin/sync-documents";
  var ROOT_ID = "syncDocumentsRoot";

  var _state = { chip: "all", classification_status: "", confidence: "", q: "" };
  var _detailId = null;

  function toast(msg, type) {
    if (window.CUS_TOAST) { window.CUS_TOAST(msg, type || "info"); return; }
    console.log("[CUS-SyncDocs] " + msg);
  }
  function token() { return localStorage.getItem("cus_admin_token") || ""; }
  function authHeaders(json) {
    var h = json ? { "Content-Type": "application/json" } : {};
    var t = token();
    if (t) h.Authorization = "Bearer " + t;
    return h;
  }
  function req(method, url, body) {
    var opts = { method: method, headers: authHeaders(body !== undefined) };
    if (body !== undefined) opts.body = JSON.stringify(body);
    return fetch(url, opts).then(function (r) {
      if (r.status === 401) { window.location.reload(); throw new Error("Unauthorized"); }
      return r.text().then(function (raw) {
        var data;
        try { data = raw ? JSON.parse(raw) : {}; } catch (e) { data = {}; }
        if (!r.ok) {
          var detail = data && data.detail ? data.detail
            : (data && data.error && data.error.message ? data.error.message : "");
          if (typeof detail === "object") detail = JSON.stringify(detail);
          throw new Error(detail || ("HTTP " + r.status));
        }
        return data;
      });
    });
  }
  function get(url) { return req("GET", url); }
  function post(url, body) { return req("POST", url, body); }

  var esc = function (s) {
    return String(s === undefined || s === null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  };

  function root() { return document.getElementById(ROOT_ID); }
  var $ = function (id) { return document.getElementById(id); };

  function statusBadge(n) {
    var cls = {
      "pending_review": "background:#fef3c7;color:#92400e;",
      "verified": "background:#eef7f1;color:#0f5132;",
      "hidden_hold": "background:#fde8e8;color:#991b1b;",
      "draft": "background:#eef2f7;color:#334155;"
    }[n.classification_status] || "background:#eef2f7;color:#334155;";
    return '<span style="display:inline-block;padding:2px 8px;border-radius:999px;font-size:11px;font-weight:600;' +
      cls + '">' + esc(n.classification_status || "draft") + "</span>";
  }

  function confBadge(c) {
    if (!c) return "";
    var cls = { high: "#0f5132;background:#eef7f1", medium: "#92400e;background:#fef3c7", low: "#991b1b;background:#fde8e8" }[c.band] || "#334155;background:#eef2f7";
    return '<span style="display:inline-block;padding:2px 8px;border-radius:999px;font-size:11px;font-weight:600;color:' +
      cls + '">' + esc(c.band) + (typeof c.score === "number" ? " " + c.score : "") + "</span>";
  }

  // Human-readable category labels. Internal values stay intact (API + data);
  // these are presentation labels consistent with the review categories.
  function catLabel(p) {
    if (!p) return "—";
    if (p.doc_type === "knowledge") return "Knowledge";
    if (p.doc_type === "ambiguous") return "Needs Review";
    return {
      "date-sheet": "Date Sheet",
      "model-paper": "Model Question Paper",
      "official-notification": "Official Notification",
      "other-official-document": "Other Official Document"
    }[p.category] || p.category || "—";
  }

  function docTypeLabel(p) {
    return { official: "Official", knowledge: "Knowledge", ambiguous: "Ambiguous" }[p && p.doc_type] || (p && p.doc_type) || "—";
  }

  // Category chips mirror the list endpoint's disjoint category predicates.
  // [key, label, count-key-in-stats]
  var CHIPS = [
    ["all", "All", "total_pages"],
    ["date-sheet", "Date Sheets", "date-sheet"],
    ["model-paper", "Model Question Papers", "model-paper"],
    ["official-notification", "Official Notifications", "official-notification"],
    ["other-official-document", "Other Official Documents", "other-official-document"],
    ["knowledge", "Knowledge", "knowledge"],
    ["needs-review", "Needs Review", "ambiguous"]
  ];
  var CHIP_LABEL = {};
  CHIPS.forEach(function (c) { CHIP_LABEL[c[0]] = c[1]; });

  function fetchStats() {
    return get(BASE + "/stats").catch(function () { return null; });
  }
  function fetchList() {
    var qs = [];
    // Active category chip -> disjoint predicate over the same list endpoint.
    if (_state.chip === "knowledge") qs.push("doc_type=knowledge");
    else if (_state.chip === "needs-review") qs.push("doc_type=ambiguous");
    else if (_state.chip && _state.chip !== "all") qs.push("category=" + encodeURIComponent(_state.chip));
    if (_state.classification_status) qs.push("classification_status=" + encodeURIComponent(_state.classification_status));
    if (_state.confidence) qs.push("confidence=" + encodeURIComponent(_state.confidence));
    if (_state.q) qs.push("q=" + encodeURIComponent(_state.q));
    qs.push("limit=200", "offset=0");
    return get(BASE + (qs.length ? "?" + qs.join("&") : ""));
  }

  function renderList() {
    _detailId = null;
    var h = "";
    h += '<div class="tab-head" style="display:flex;justify-content:space-between;align-items:flex-end;flex-wrap:wrap;gap:10px;">';
    h += "<div><h2 style='margin:0;'>Sync Documents &mdash; Review Queue</h2>";
    h += "<p class='sub' style='margin:4px 0 0 0;'>Classified documents from the website sync engine. Verify confirms the classification/metadata; nothing is ever auto-published.</p></div>";
    h += '<div style="display:flex;gap:8px;align-items:center;">';
    h += '<button class="btn sm ghost" id="sd_refresh">&#x21bb; Refresh</button>';
    h += "</div></div>";

    h += '<div id="sd_chips" style="display:flex;gap:8px;flex-wrap:wrap;margin-top:14px;">Loading usage categories...</div>';

    h += '<div id="sd_stats" style="display:flex;gap:10px;flex-wrap:wrap;margin-top:12px;">Loading...</div>';

    h += '<div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:14px;align-items:center;">';
    h += '<input type="text" id="sd_q" placeholder="Search URL / title..." style="flex:1;min-width:200px;padding:8px 12px;border:1px solid var(--border);border-radius:6px;font-size:14px;" />';
    h += '<select id="sd_status" style="padding:8px 10px;border:1px solid var(--border);border-radius:6px;background:var(--card);color:var(--text);">';
    h += '<option value="">All statuses</option><option value="pending_review">Pending review</option><option value="verified">Verified</option><option value="hidden_hold">Hidden / hold</option><option value="draft">Draft</option></select>';
    h += '<select id="sd_confidence" style="padding:8px 10px;border:1px solid var(--border);border-radius:6px;background:var(--card);color:var(--text);">';
    h += '<option value="">All confidence</option><option value="high">High</option><option value="medium">Medium</option><option value="low">Low</option></select>';
    h += '<button class="btn sm ghost" id="sd_apply">Apply</button>';
    h += "</div>";

    h += '<div id="sd_rows" style="margin-top:14px;">Loading...</div>';
    root().innerHTML = h;

    // Restore the combined filter inputs so chip/search/status/confidence
    // state persists across re-renders.
    $("sd_q").value = _state.q || "";
    $("sd_status").value = _state.classification_status || "";
    $("sd_confidence").value = _state.confidence || "";

    $("sd_refresh").addEventListener("click", refresh);
    $("sd_apply").addEventListener("click", apply);
    $("sd_q").addEventListener("keydown", function (e) { if (e.key === "Enter") apply(); });
    load();
  }

  function apply() {
    _state.q = $("sd_q").value || "";
    _state.classification_status = $("sd_status").value || "";
    _state.confidence = $("sd_confidence").value || "";
    renderList();
  }
  function refresh() { renderList(); }

  function load() {
    fetchStats().then(function (stats) {
      var chipsEl = $("sd_chips");
      if (chipsEl) {
        if (!stats) {
          chipsEl.innerHTML = '<span class="muted">Category counts unavailable</span>';
        } else {
          var cats = stats.by_category || {};
          chipsEl.innerHTML = CHIPS.map(function (c) {
            var key = c[0], label = c[1];
            var count = key === "all" ? (stats.total_pages || 0) : (cats[c[2]] != null ? cats[c[2]] : 0);
            var active = _state.chip === key;
            return '<button class="sd-chip" data-chip="' + key + '" type="button" style="' +
              (active ? "background:#0f5132;color:#ffffff;border-color:#0f5132;" : "background:var(--card);color:var(--text);border-color:var(--line);") +
              'border:1px solid;padding:6px 12px;border-radius:999px;font-size:13px;cursor:pointer;">' +
              esc(label) + ' <b style="opacity:.85;">' + count + "</b></button>";
          }).join("");
          chipsEl.querySelectorAll(".sd-chip").forEach(function (b) {
            b.addEventListener("click", function () {
              _state.chip = b.getAttribute("data-chip");
              apply();
            });
          });
        }
      }
      var el = $("sd_stats");
      if (!el) return;
      if (!stats) { el.innerHTML = '<span class="muted">Stats unavailable</span>'; return; }
      var chips = [
        ["Pending review", stats.pending_review, "#fef3c7;color:#92400e"],
        ["Verified", stats.verified, "#eef7f1;color:#0f5132"],
        ["Hidden / hold", stats.hidden_hold, "#fde8e8;color:#991b1b"],
        ["Total pages", stats.total_pages, "#eef2f7;color:#334155"]
      ];
      el.innerHTML = chips.map(function (c) {
        return '<div style="padding:10px 14px;border:1px solid var(--line);border-radius:8px;background:var(--bg);">' +
          '<div style="font-size:20px;font-weight:700;color:' + c[2].split(";")[1] + ';">' + c[1] + "</div>" +
          '<div class="muted" style="font-size:12px;">' + esc(c[0]) + "</div></div>";
      }).join("");
    }).catch(function () {});
    fetchList().then(renderRows).catch(function (err) {
      var el = $("sd_rows");
      if (el) el.innerHTML = '<p class="muted">Failed to load: ' + esc(err.message) + "</p>";
    });
  }

  function renderRows(data) {
    var el = $("sd_rows");
    if (!el) return;
    var items = (data && data.items) || [];
    var head = '<div class="sync-log" style="max-height:560px;overflow:auto;">' +
      '<table style="width:100%;border-collapse:collapse;font-size:13px;">' +
      "<thead><tr style='text-align:left;'>" +
      "<th>Title</th><th>Type</th><th>Category</th><th>Confidence</th><th>Status</th><th>Reviewed</th><th>Raw</th><th></th>" +
      "</tr></thead><tbody>";
    var rows = items.map(function (p) {
      var conf = p.classification_confidence || {};
      return "<tr style='border-top:1px solid var(--line);'>" +
        "<td style='padding:8px 6px;'><div style='max-width:280px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;' title='" + esc(p.url) + "'>" + esc(p.title || p.url) + "</div><div class='muted' style='font-size:11px;'>" + esc(p.content_type) + "</div></td>" +
        "<td style='padding:8px 6px;'>" + docTypeLabel(p) + "</td>" +
        "<td style='padding:8px 6px;' title='" + esc(p.category || "") + "'>" + esc(catLabel(p)) + "</td>" +
        "<td style='padding:8px 6px;'>" + confBadge(conf) + "</td>" +
        "<td style='padding:8px 6px;'>" + statusBadge(p) + "</td>" +
        "<td style='padding:8px 6px;'>" + esc(p.reviewed_by || "â€”") + "</td>" +
        "<td style='padding:8px 6px;'>" + (p.raw_sha256 ? "&#x1f4be;" : "â€”") + "</td>" +
        "<td style='padding:8px 6px;'><button class='btn sm ghost' data-sdview='" + esc(p.id) + "'>View</button></td>" +
        "</tr>";
    }).join("");
    var foot = "</tbody></table></div>";
    el.innerHTML = head + rows + foot;
    el.querySelectorAll("[data-sdview]").forEach(function (btn) {
      btn.addEventListener("click", function () { loadDetail(btn.getAttribute("data-sdview")); });
    });
  }

  function loadDetail(id) {
    get(BASE + "/" + encodeURIComponent(id)).then(function (p) {
      _detailId = id;
      renderDetail(p);
    }).catch(function (err) { toast(err.message, "error"); });
  }

  function renderDetail(p) {
    var h = "";
    h += '<div class="admin-card">';
    h += '<div style="display:flex;justify-content:space-between;align-items:flex-start;flex-wrap:wrap;gap:8px;">';
    h += "<div><button class='btn sm ghost' id='sd_back'>&larr; Back</button></div>";
    h += statusBadge(p);
    h += "</div>";
    h += "<h3 style='margin:12px 0 4px 0;'>" + esc(p.title || p.url) + "</h3>";
    h += '<div class="sub" style="margin:0 0 10px 0;word-break:break-all;">' + esc(p.url) + "</div>";
    h += '<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:10px;margin-bottom:12px;">';
    var cells = [
      ["Doc type", docTypeLabel(p)],
      ["Category", catLabel(p) + (p.category ? " (" + p.category + ")" : "")],
      ["Content type", p.content_type || "â€”"],
      ["Raw preserved", p.raw_sha256 ? "Yes (on disk: " + (p.has_raw_on_disk ? "yes" : "NO") + ")" : "No"],
      ["Raw SHA-256", p.raw_sha256 ? p.raw_sha256.slice(0, 16) + "â€¦" : "â€”"],
      ["Raw size", p.raw_size != null ? Math.round(p.raw_size / 1024) + " KB" : "â€”"],
      ["Status", p.status || "â€”"],
      ["Last synced", p.last_synced || "â€”"],
      ["Reviewed by", p.reviewed_by || "â€”"],
      ["Reviewed at", p.reviewed_at || "â€”"]
    ];
    cells.forEach(function (c) {
      h += '<div style="padding:8px 10px;border:1px solid var(--line);border-radius:8px;background:var(--bg);"><div class="muted" style="font-size:11px;">' + esc(c[0]) + '</div><div style="font-size:13px;">' + esc(c[1]) + "</div></div>";
    });
    h += "</div>";

    h += '<div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px;">';
    h += '<button class="btn green" id="sd_verify">Verify</button>';
    h += '<button class="btn ghost" id="sd_ambiguous">Mark Ambiguous</button>';
    h += '<button class="btn ghost" id="sd_hide">Hide</button>';
    h += '<button class="btn ghost" id="sd_reprocess">Reprocess</button>';
    h += "</div>";
    h += '<div style="margin-bottom:10px;"><input type="text" id="sd_note" placeholder="Review note (optional)" style="width:100%;padding:8px 12px;border:1px solid var(--border);border-radius:6px;" /></div>';

    h += '<div class="muted" style="font-size:12px;margin-bottom:8px;">Classification signals</div>';
    if ((p.classification_signals || []).length) {
      h += "<ul style='margin:0 0 12px 0;padding-left:18px;'>" + p.classification_signals.map(function (s) { return "<li>" + esc(s) + "</li>"; }).join("") + "</ul>";
    } else {
      h += '<p class="sub">No signals recorded.</p>';
    }

    h += '<div class="muted" style="font-size:12px;margin-bottom:8px;">Metadata</div>';
    h += '<pre class="sync-log" style="max-height:220px;overflow:auto;font-size:12px;white-space:pre-wrap;">' + esc(JSON.stringify(p.doc_meta || {}, null, 2)) + "</pre>";

    h += '<div class="muted" style="font-size:12px;margin-bottom:8px;margin-top:12px;">Version history</div>';
    var ver = (p.versions || []).map(function (v) {
      return "<div style='font-size:12px;padding:4px 0;border-top:1px solid var(--line);'>v" + esc(v.version) +
        " &middot; " + esc(v.synced_at || "") +
        (v.raw_sha256 ? " &middot; raw preserved" : "") +
        (v.content_hash ? " &middot; sha=" + esc(v.content_hash.slice(0, 10)) : "") + "</div>";
    }).join("");
    h += ver || '<p class="sub">No archived versions.</p>';

    h += "</div>";
    root().innerHTML = h;

    $("sd_back").addEventListener("click", renderList);
    $("sd_verify").addEventListener("click", function () { act("verify", { review_note: note() }); });
    $("sd_ambiguous").addEventListener("click", function () { act("ambiguous", { review_note: note() }); });
    $("sd_hide").addEventListener("click", function () { act("hide", { review_note: note() }); });
    $("sd_reprocess").addEventListener("click", function () { act("reprocess", {}); });
  }

  function note() {
    var el = $("sd_note");
    return el ? el.value : "";
  }

  function act(action, body) {
    if (!_detailId) return;
    post(BASE + "/" + encodeURIComponent(_detailId) + "/" + action, body).then(function () {
      toast("Action '" + action + "' applied", "success");
      if (action === "reprocess") { loadDetail(_detailId); return; }
      loadDetail(_detailId);
    }).catch(function (err) { toast(err.message, "error"); });
  }

  function init() { renderList(); }
  window.CUS = window.CUS || {};
  window.CUS.syncDocumentsInit = init;
})();