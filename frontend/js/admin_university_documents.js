/* =====================================================================
 * admin_university_documents.js  —  Consolidated "University Documents"
 * administrator panel (Phase 3C-7 FINAL).
 *
 * ONE canonical location for the university document repository. Both
 * crawler output and manual uploads live in the SAME table
 * (`university_documents`) — `source` (crawler | manual_upload) is
 * INDEPENDENT of `doc_type` (date_sheet | model_paper |
 * official_notification | other_official_document | knowledge).
 *
 * This module REPLACES the former separate admin presentations
 * ("Universal Notices" + "Sync Documents") which used a separate
 * `universal_notices` table and a separate crawler-only view.
 *
 * Student-facing behaviour (date sheets, model papers, notifications,
 * exam services, RAG) is unchanged — the backend seams stay intact.
 * ===================================================================== */

(function () {
  "use strict";

  if (!window.CUS_API_BASE) throw new Error("CUS_API_BASE not defined");
  var API = window.CUS_API_BASE;

  var CATEGORIES = [
    { v: "",        label: "All" },
    { v: "date_sheet",               label: "Date Sheets" },
    { v: "model_paper",              label: "Model Question Papers" },
    { v: "official_notification",    label: "Official Notifications" },
    { v: "other_official_document",  label: "Other Official Documents" },
    { v: "knowledge",                label: "Knowledge" }
  ];
  var REVIEW_CAT = { v: "needs_review", label: "Needs Review" };

  var SOURCES = [
    { v: "", label: "All Sources" },
    { v: "crawler", label: "Crawler" },
    { v: "manual_upload", label: "Manual Upload" }
  ];

  var STATUSES = [
    { v: "", label: "All Status" },
    { v: "pending_review", label: "Pending Review" },
    { v: "needs_review", label: "Needs Review" },
    { v: "verified", label: "Verified" },
    { v: "published", label: "Published" },
    { v: "hidden_hold", label: "Hidden / Hold" }
  ];

  var CONFIDENCE = [
    { v: "", label: "All Confidence" },
    { v: "low", label: "\u2265 30%" },
    { v: "50", label: "\u2265 50%" },
    { v: "75", label: "\u2265 75%" },
    { v: "high", label: "\u2265 90%" }
  ];

  var STATE = { cat: "", source: "", status: "", conf: "", q: "", offset: 0, limit: 25, loading: false };
  var ROOT_ID = "universityDocumentsRoot";

  /* ---------- small helpers (self-contained; do not collide) ---------- */
  function $(id) { return document.getElementById(id); }
  function esc(s) {
    if (typeof s !== "string") s = String(s == null ? "" : s);
    return s.replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function token() { return localStorage.getItem("cus_admin_token") || ""; }
  function authHeaders(json) {
    var h = {};
    if (token()) h.Authorization = "Bearer " + token();
    if (json) h["Content-Type"] = "application/json";
    return h;
  }
  function toast(msg, type) {
    if (window.CUS_TOAST) { window.CUS_TOAST(msg, type || "info"); return; }
    if (window.console) console.log("[UniversityDocs] " + msg);
  }
  function fmtSize(b) {
    if (!b && b !== 0) return "-";
    if (b < 1024) return b + " B";
    if (b < 1048576) return (b / 1024).toFixed(1) + " KB";
    return (b / 1048576).toFixed(1) + " MB";
  }
  function pct(p) {
    if (p == null) return "-";
    if (typeof p === "object") {
      var s = p.score != null ? Number(p.score) : NaN;
      if (!isNaN(s)) return (p.band ? p.band + " \u00b7 " : "") + Math.round(s) + "%";
      return esc(String(p.band || "-"));
    }
    var n = parseFloat(p);
    if (isNaN(n)) return esc(String(p));
    return Math.round(n * 100) + "%";
  }
  function catLabel(v) {
    for (var i = 0; i < CATEGORIES.length; i++) if (CATEGORIES[i].v === v) return CATEGORIES[i].label;
    return esc(v || "-");
  }
  function sourceLabel(v) {
    if (v === "crawler") return '<span class="pill grey">Crawler</span>';
    if (v === "manual_upload" || v === "manual_upload") return '<span class="pill blue">Manual</span>';
    return esc(v || "-");
  }
  function statusLabel(d) {
    var v = (d.status || "").toLowerCase();
    if (d.is_published || d.isPublished || v === "published") return '<span class="pill green">Published</span>';
    if (v === "verified" || d.is_verified || d.isVerified) return '<span class="pill blue">Verified</span>';
    if (v === "needs_review") return '<span class="pill amber">Needs Review</span>';
    if (v === "hidden_hold" || v === "hidden") return '<span class="pill grey">Hidden</span>';
    if (v === "pending_review") return '<span class="pill amber">Pending Review</span>';
    return '<span class="pill grey">' + esc(v || "-") + "</span>";
  }
  function fileInfo(d) {
    if (d && d.file) return d.file;
    return d || {};
  }

  /* ---------- request helpers ---------- */
  function listUrl() {
    var p = ["limit=" + STATE.limit, "offset=" + STATE.offset];
    if (STATE.cat) p.push("doc_type=" + encodeURIComponent(STATE.cat));
    if (STATE.status) p.push("status=" + encodeURIComponent(STATE.status));
    if (STATE.source) p.push("source=" + encodeURIComponent(STATE.source));
    if (STATE.q) p.push("q=" + encodeURIComponent(STATE.q));
    return API + "/api/admin/university-documents?" + p.join("&");
  }
  function apiReq(method, path, body) {
    var opts = { method: method, headers: authHeaders(true) };
    if (body !== undefined) opts.body = JSON.stringify(body);
    return fetch(path, opts).then(function (r) {
      if (r.status === 401) { redirectLogin(); throw new Error("unauthorized"); }
      return r.json().catch(function () { return {}; }).then(function (data) {
        if (!r.ok) {
          var d = data && (data.detail || (data.error && data.error.message));
          if (d && typeof d === "object") d = JSON.stringify(d);
          throw new Error(d || ("HTTP " + r.status));
        }
        return data;
      });
    });
  }
  function redirectLogin() {
    if (window.location && window.location.search.indexOf("page=admin") < 0) {
      window.location.href = window.location.pathname + "?page=admin";
    }
  }

  /* ---------- rendering ---------- */
  function toolbar() {
    var cats = "";
    for (var i = 0; i < CATEGORIES.length; i++) {
      var c = CATEGORIES[i];
      cats += '<button class="ud-cat' + (STATE.cat === c.v ? " active" : "") + '" data-v="' + esc(c.v) + '">' + esc(c.label) + "</button>";
    }
    cats += '<button class="ud-cat' + (STATE.cat === REVIEW_CAT.v ? " active" : "") + '" data-v="needs_review">Needs Review</button>';

    var srcs = '<select id="udSourceF" class="ud-sel">';
    for (var s = 0; s < SOURCES.length; s++) {
      srcs += '<option value="' + esc(SOURCES[s].v) + '"' + (STATE.source === SOURCES[s].v ? " selected" : "") + ">" + esc(SOURCES[s].label) + "</option>";
    }
    srcs += "</select>";

    var sts = '<select id="udStatusF" class="ud-sel">';
    for (var t = 0; t < STATUSES.length; t++) {
      sts += '<option value="' + esc(STATUSES[t].v) + '"' + (STATE.status === STATUSES[t].v ? " selected" : "") + ">" + esc(STATUSES[t].label) + "</option>";
    }
    sts += "</select>";

    var cfs = '<select id="udConfF" class="ud-sel">';
    for (var cf = 0; cf < CONFIDENCE.length; cf++) {
      cfs += '<option value="' + esc(CONFIDENCE[cf].v) + '"' + (STATE.conf === CONFIDENCE[cf].v ? " selected" : "") + ">" + esc(CONFIDENCE[cf].label) + "</option>";
    }
    cfs += "</select>";

    return '<div class="ud-bar">'
      + '<div class="ud-cats">' + cats + "</div>"
      + '<div class="ud-filters">'
      + '<input id="udQ" class="ud-inp" type="text" placeholder="Search title / file / notice\u2026" value="' + esc(STATE.q) + '" />'
      + srcs + sts + cfs
      + '<button class="btn sm" id="udUploadBtn">\u2795 Upload Document</button>'
      + "</div>"
      + "</div>";
  }

  function tableHead() {
    return "<thead><tr>"
      + "<th>Title</th><th>Source</th><th>Type</th>"
      + "<th>Confidence</th><th>Status</th><th>Updated</th><th>Actions</th>"
      + "</tr></thead>";
  }

  function actionButtons(d) {
    var id = esc(d.id);
    var b = '<button class="btn sm ghost" data-op="view" data-id="' + id + '">View</button> ';
    if (!d.is_verified && !d.isVerified) {
      b += '<button class="btn sm green" data-op="verify" data-id="' + id + '">Verify</button> ';
    }
    if (!d.is_published && !d.isPublished && (d.is_verified || d.isVerified)) {
      b += '<button class="btn sm blue" data-op="publish" data-id="' + id + '">Publish</button> ';
    }
    if (d.is_published || d.isPublished) {
      b += '<button class="btn sm ghost" data-op="unpublish" data-id="' + id + '">Unpublish</button> ';
    }
    if ((d.status || "").toLowerCase() === "hidden_hold" || (d.status || "").toLowerCase() === "hidden") {
      b += '<button class="btn sm ghost" data-op="restore" data-id="' + id + '">Restore</button> ';
    } else {
      b += '<button class="btn sm ghost" data-op="hide" data-id="' + id + '">Hide</button> ';
    }
    b += '<button class="btn sm ghost" data-op="reclassify" data-id="' + id + '">Reclassify</button> ';
    b += '<button class="btn sm ghost danger" data-op="trash" data-id="' + id + '">Delete</button>';
    return b;
  }

  function row(d) {
    var f = fileInfo(d);
    var title = d.title || f.original_filename || "(untitled)";
    var updated = d.updated_at || d.created_at || "";
    if (updated) updated = String(updated).slice(0, 16).replace("T", " ");
    return "<tr>"
      + "<td><strong>" + esc(title) + "</strong>"
      + (f.original_filename ? '<div class="ud-sub">' + esc(f.original_filename) + "</div>" : "")
      + "</td>"
      + "<td>" + sourceLabel(d.source) + "</td>"
      + "<td>" + esc(catLabel(d.doc_type)) + "</td>"
      + "<td>" + pct(d.confidence) + "</td>"
      + "<td>" + statusLabel(d) + "</td>"
      + "<td>" + esc(updated) + "</td>"
      + "<td>" + actionButtons(d) + "</td>"
      + "</tr>";
  }

  function render() {
    var root = $(ROOT_ID);
    if (!root) return;
    var html = toolbar()
      + '<div class="ud-toolbar-note">Both crawled and manually uploaded university documents are listed here in one canonical repository. Use <strong>Website Sync</strong> to control the crawler.</div>'
      + '<table class="ud-table">' + tableHead()
      + '<tbody id="udBody"><tr><td colspan="7" class="ud-empty">Loading\u2026</td></tr></tbody>'
      + "</table>"
      + '<div class="ud-paging"><button class="btn sm ghost" id="udPrev">\u2190 Prev</button><span id="udRange"></span><button class="btn sm ghost" id="udNext">Next \u2192</button></div>';
    root.innerHTML = html;
    bindToolbar();
    loadList();
  }

  function bindToolbar() {
    var cats = document.querySelectorAll("#" + ROOT_ID + " .ud-cat");
    for (var i = 0; i < cats.length; i++) {
      (function (el) { el.addEventListener("click", function () { STATE.cat = this.dataset.v; STATE.offset = 0; render(); }); })(cats[i]);
    }
    $("udSourceF").addEventListener("change", function () { STATE.source = this.value; STATE.offset = 0; loadList(); });
    $("udStatusF").addEventListener("change", function () { STATE.status = this.value; STATE.offset = 0; loadList(); });
    $("udConfF").addEventListener("change", function () { STATE.conf = this.value; STATE.offset = 0; loadList(); });
    $("udQ").addEventListener("keydown", function (e) { if (e.key === "Enter") { STATE.q = this.value; STATE.offset = 0; loadList(); } });
    $("udUploadBtn").addEventListener("click", openUpload);
    $("udPrev").addEventListener("click", function () { if (STATE.offset > 0) { STATE.offset -= STATE.limit; if (STATE.offset < 0) STATE.offset = 0; loadList(); } });
    $("udNext").addEventListener("click", function () { STATE.offset += STATE.limit; loadList(); });
  }

  function bindRows() {
    var btns = document.querySelectorAll("#" + ROOT_ID + " [data-op]");
    for (var i = 0; i < btns.length; i++) {
      (function (el) {
        el.addEventListener("click", function () {
          var op = el.dataset.op, id = el.dataset.id;
          if (op === "view") viewDoc(id);
          else if (op === "verify") act(id, "verify");
          else if (op === "publish") act(id, "publish");
          else if (op === "unpublish") act(id, "unpublish");
          else if (op === "hide") act(id, "hide");
          else if (op === "restore") act(id, "restore");
          else if (op === "trash") trash(id);
          else if (op === "reclassify") reclassify(id);
        });
      })(btns[i]);
    }
  }

  function loadList() {
    if (STATE.loading) return;
    STATE.loading = true;
    $("udPrev").disabled = true; $("udNext").disabled = true;
    fetch(listUrl(), { headers: authHeaders() }).then(function (r) {
      if (r.status === 401) { redirectLogin(); throw new Error("unauthorized"); }
      return r.json();
    }).catch(function () { return { items: [], total: 0 }; }).then(function (data) {
      STATE.loading = false;
      var items = (data && data.items) || [];
      var total = data && typeof data.total === "number" ? data.total : items.length;
      var body = $("udBody");
      if (!body) return;
      if (!items.length) { body.innerHTML = '<tr><td colspan="7" class="ud-empty">No university documents match the current filters.</td></tr>'; }
      else {
        var h = "";
        for (var i = 0; i < items.length; i++) h += row(items[i]);
        body.innerHTML = h;
      }
      bindRows();
      var firstShown = items.length ? STATE.offset + 1 : 0;
      $("udRange").textContent = firstShown + "\u2013" + (STATE.offset + items.length) + " of " + total;
      $("udPrev").disabled = STATE.offset <= 0;
      $("udNext").disabled = STATE.offset + items.length >= total;
    });
  }

  function act(id, op) {
    var label = op.charAt(0).toUpperCase() + op.slice(1);
    apiReq("POST", API + "/api/admin/university-documents/" + encodeURIComponent(id) + "/" + op, {})
      .then(function () { toast(label + " succeeded \u2713", "success"); loadList(); })
      .catch(function (e) { toast(label + " failed: " + e.message, "error"); });
  }

  function trash(id) {
    if (!window.confirm("Permanently soft-delete this document? It can be restored later from the repository.")) return;
    apiReq("DELETE", API + "/api/admin/university-documents/" + encodeURIComponent(id))
      .then(function () { toast("Document deleted \u2713", "success"); loadList(); })
      .catch(function (e) { toast("Delete failed: " + e.message, "error"); });
  }

  function viewDoc(id) {
    apiReq("GET", API + "/api/admin/university-documents/" + encodeURIComponent(id)).then(function (d) {
      var f = fileInfo(d);
      var rows = [
        ["Title", d.title || "-"],
        ["Type", catLabel(d.doc_type)],
        ["Source", d.source === "crawler" ? "Crawler" : d.source === "manual_upload" ? "Manual Upload" : esc(d.source || "-")],
        ["File", f.file_path || d.file_path || "-"],
        ["Original name", f.original_filename || d.original_filename || "-"],
        ["Type / Size", (f.file_type || "-") + " / " + fmtSize(f.file_size != null ? f.file_size : d.file_size)],
        ["SHA-256", (f.sha256 || d.sha256 || "-")],
        ["Source URL", d.source_url || "-"],
        ["Status", (d.status || "-")],
        ["Verified", d.is_verified || d.isVerified ? "Yes" : "No"],
        ["Published", d.is_published || d.isPublished ? "Yes" : "No"],
        ["Confidence", pct(d.confidence)],
        ["Created", d.created_at || "-"]
      ];
      var sigs = (d.provenance && d.provenance.signals) || [];
      var html = '<button class="btn sm ghost" id="udBack">\u2190 Back</button>'
        + "<h3>" + esc(d.title || "(untitled)") + "</h3>"
        + '<table class="ud-table"><tbody>';
      for (var i = 0; i < rows.length; i++) {
        html += "<tr><th>" + rows[i][0] + "</th><td>" + rows[i][1] + "</td></tr>";
      }
      html += "</tbody></table>";
      if (sigs.length) {
        html += '<div class="muted" style="font-size:12px;margin-top:10px;">Classification signals</div>';
        html += "<ul style='margin:4px 0 0 0;padding-left:18px;'>" + sigs.map(function (s) { return "<li>" + esc(s) + "</li>"; }).join("") + "</ul>";
      }
      var root = $(ROOT_ID);
      root.innerHTML = html;
      $("udBack").addEventListener("click", render);
    }).catch(function (e) { toast("Failed to load document: " + e.message, "error"); });
  }

  function reclassify(id) {
    var v = window.prompt("New document type:\n\n1 = Date Sheet\n2 = Model Question Paper\n3 = Official Notification\n4 = Other Official Document\n5 = Knowledge");
    if (!v) return;
    var path = { "1": "date_sheet", "2": "model_paper", "3": "official_notification", "4": "other_official_document", "5": "knowledge" }[String(v).trim()];
    if (!path) { toast("Invalid choice. Use 1\u20135.", "error"); return; }
    apiReq("POST", API + "/api/admin/university-documents/" + encodeURIComponent(id) + "/reclassify", { doc_type: path })
      .then(function () { toast("Reclassified \u2713", "success"); loadList(); })
      .catch(function (e) { toast("Reclassify failed: " + e.message, "error"); });
  }

  /* ---------- upload ---------- */
  function openUpload() {
    var root = $(ROOT_ID);
    var html = '<button class="btn sm ghost" id="udBack">\u2190 Back</button>'
      + "<h3>Upload University Document</h3>"
      + '<form id="udUpForm">'
      + '<label>Title *<br><input type="text" id="udTitle" class="ud-inp" required /></label>'
      + '<label>Category *<br><select id="udDocType" class="ud-sel">'
      + '<option value="date_sheet">Date Sheet</option>'
      + '<option value="model_paper">Model Question Paper</option>'
      + '<option value="official_notification">Official Notification</option>'
      + '<option value="other_official_document">Other Official Document</option>'
      + '<option value="knowledge">Knowledge</option>'
      + "</select></label>"
      + '<label>File *<br><input type="file" id="udFile" class="ud-inp" required /></label>'
      + '<button type="submit" class="btn green">Upload</button>'
      + '<button type="button" class="btn ghost" id="udCancel">Cancel</button>'
      + "</form>";
    root.innerHTML = html;
    $("udBack").addEventListener("click", render);
    $("udCancel").addEventListener("click", render);
    $("udUpForm").addEventListener("submit", function (e) {
      e.preventDefault();
      var file = $("udFile").files[0];
      if (!file) { toast("Choose a file", "error"); return; }
      var fd = new FormData();
      fd.append("file", file);
      fd.append("title", $("udTitle").value.trim());
      fd.append("doc_type", $("udDocType").value);
      fetch(API + "/api/admin/university-documents", { method: "POST", headers: authHeaders(false), body: fd })
        .then(function (r) { return r.json().catch(function () { return {}; }).then(function (j) { if (!r.ok) throw new Error(j.detail || ("HTTP " + r.status)); return j; }); })
        .then(function () { toast("Document uploaded \u2713", "success"); render(); })
        .catch(function (e) { toast("Upload failed: " + e.message, "error"); });
    });
  }

  /* ---------- public API ---------- */
  window.CUS = window.CUS || {};
  window.CUS.universityDocumentsInit = function () { STATE.offset = 0; render(); };

  if (window.CUS_ADMIN_READY) window.CUS.universityDocumentsInit();
})();

/* =====================================================================
 * NOTE ON DELETION / REMOVAL OF LEGACY PRESENTATIONS
 * ---------------------------------------------------------------------
 * - The old admin presentations (the separate "Universal Notices" admin
 *   view and the separate "Sync Documents" crawler view that used the
 *   `universal_notices` table) are superseded by THIS module. Their old
 *   admin nav buttons and standalone panels are removed from admin.html;
 *   the new single canonical "University Documents" entry in the admin
 *   nav points here.
 * - Internal compatibility is preserved: the legacy backend tables/routes
 *   are intentionally left intact so student-facing features (notices,
 *   date sheets, model papers, exam services, RAG) keep working. Only the
 *   DUPLICATE administrator presentation is consolidated.
 * ===================================================================== */
