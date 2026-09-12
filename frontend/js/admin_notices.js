/**
 * admin_notices.js — Super-Admin management of public university notices and
 * date sheets. Backs the admin "University Notices" tab.
 *
 * Lifecycle (mirrors backend/app/notices): upload+extract -> review/correct
 * schedule entries -> verify -> publish. Only verified + published notices
 * are served to the public chatbot.
 */
(function () {
  "use strict";

  if (!window.CUS_API_BASE) throw new Error("CUS_API_BASE not defined");
  var API = window.CUS_API_BASE;
  var BASE = API + "/api/admin/notices";
  var ROOT_ID = "noticesAdminRoot";

  var _state = { q: "", notice_type: "", status: "" };
  var _detailId = null;
  var _page = 1;
  var _pageSize = 100;
  var _lock = false;

  function toast(msg, type) {
    if (window.CUS_TOAST) { window.CUS_TOAST(msg, type || "info"); return; }
    console.log("[CUS-Notices] " + msg);
  }

  function token() { return localStorage.getItem("cus_admin_token") || ""; }

  function authHeaders(json) {
    var h = json ? { "Content-Type": "application/json" } : {};
    var t = token();
    if (t) h.Authorization = "Bearer " + t;
    return h;
  }

  function req(method, url, body, isForm) {
    var opts = { method: method, headers: authHeaders(!isForm) };
    if (body !== undefined && !isForm) opts.body = JSON.stringify(body);
    if (body !== undefined && isForm) opts.body = body; // FormData (browser sets boundary)
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
  function post(url, body, isForm) { return req("POST", url, body, isForm); }
  function patch(url, body) { return req("PATCH", url, body); }
  function del(url) { return req("DELETE", url); }

  var esc = function (s) {
    return String(s === undefined || s === null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  };

  function root() { return document.getElementById(ROOT_ID); }

  function statusBadge(n) {
    var parts = [];
    parts.push('<span class="aa-stat-label" style="' +
      "display:inline-block;padding:2px 8px;border-radius:999px;font-size:11px;font-weight:600;margin-right:6px;" +
      "background:#eef7f1;color:#0f5132;\">" + esc(n.extraction_status || "draft") + "</span>");
    if (n.is_verified) {
      parts.push('<span style="display:inline-block;padding:2px 8px;border-radius:999px;font-size:11px;font-weight:600;margin-right:6px;background:#fef3c7;color:#92400e;">verified</span>');
    }
    if (n.is_published) {
      parts.push('<span style="display:inline-block;padding:2px 8px;border-radius:999px;font-size:11px;font-weight:600;margin-right:6px;background:#dbeafe;color:#1e40af;">published</span>');
    }
    return parts.join("");
  }

  /* ------------------------------------------------------------------ */
  /* List + upload                                                       */
  /* ------------------------------------------------------------------ */

  function renderList() {
    _detailId = null;
    var h = "";
    h += '<div class="tab-head" style="display:flex;justify-content:space-between;align-items:flex-end;flex-wrap:wrap;gap:10px;">';
    h += "<div><h2 style='margin:0;'>University Notices &amp; Date Sheets</h2>";
    h += "<p class='sub' style='margin:4px 0 0 0;'>Upload a PDF/DOCX, then verify + publish. Only verified &amp; published notices reach the public chatbot.</p></div>";
    h += '<div style="display:flex;gap:8px;align-items:center;">';
    h += '<button class="btn sm ghost" id="nt_uploadToggle">+ Upload Notice</button>';
    h += '<button class="btn sm ghost" id="nt_refresh">&#x21bb; Refresh</button>';
    h += "</div></div>";

    h += '<div id="nt_uploadPanel" style="display:none;margin-top:14px;">' +
      '<div class="admin-card" style="padding:16px;">' +
      '<h3 style="margin:0 0 10px 0;">Upload PDF / DOCX</h3>' +
      '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:10px;">' +
      '<label class="st-label">File * <input type="file" id="nt_file" accept=".pdf,.docx" required></label>' +
      '<label class="st-label">Title <input type="text" id="nt_title" placeholder="Optional (defaults to filename)"></label>' +
      '<label class="st-label">Type <select id="nt_type"><option value="date_sheet">date_sheet</option><option value="notice">notice</option></select></label>' +
      '<label class="st-label">Programmes (JSON array) <input type="text" id="nt_progs" placeholder=\'["bca","mca"]\'></label>' +
      '<label class="st-label">Categories (JSON array) <input type="text" id="nt_cats" placeholder=\'["examination"]\'></label>' +
      "</div>" +
      '<div style="margin-top:12px;display:flex;gap:10px;align-items:center;">' +
      '<button class="btn green" id="nt_uploadBtn">Upload &amp; Extract</button>' +
      '<span class="muted" id="nt_uploadStatus" style="font-size:13px;"></span>' +
      "</div></div></div>";

    h += '<div style="display:flex;gap:8px;flex-wrap:wrap;margin:16px 0 12px;">';
    h += '<input type="text" id="nt_q" placeholder="Search by title..." style="flex:1;min-width:200px;padding:8px 12px;border:1px solid var(--border);border-radius:6px;font-size:14px;" value="' + esc(_state.q) + '">';
    h += '<select id="nt_kind" style="padding:8px 10px;border:1px solid var(--border);border-radius:6px;">' +
      '<option value="">All types</option><option value="date_sheet">date_sheet</option><option value="notice">notice</option></select>';
    h += '<select id="nt_status" style="padding:8px 10px;border:1px solid var(--border);border-radius:6px;">' +
      '<option value="">All statuses</option><option value="draft">draft</option><option value="extracting">extracting</option>' +
      '<option value="pending_verification">pending_verification</option><option value="verified">verified</option>' +
      '<option value="extraction_failed">extraction_failed</option><option value="manual_entry">manual_entry</option></select>';
    h += '<button class="btn sm ghost" id="nt_apply">Apply</button>';
    h += "</div>";

    h += '<div id="nt_list" style="min-height:60px;"><div class="auth-loading">Loading notices...</div></div>';
    root().innerHTML = h;

    document.getElementById("nt_refresh").addEventListener("click", function () { reloadList(); });
    document.getElementById("nt_uploadToggle").addEventListener("click", function () {
      var p = document.getElementById("nt_uploadPanel");
      p.style.display = p.style.display === "none" ? "block" : "none";
    });
    document.getElementById("nt_uploadBtn").addEventListener("click", uploadNotice);
    document.getElementById("nt_apply").addEventListener("click", function () {
      _state.q = document.getElementById("nt_q").value.trim();
      _state.notice_type = document.getElementById("nt_kind").value;
      _state.status = document.getElementById("nt_status").value;
      _page = 1;
      reloadList();
    });

    loadList();
  }

  function reloadList() { loadList(); }

  function loadList() {
    var el = document.getElementById("nt_list");
    var params = ["page=1", "page_size=" + _pageSize];
    if (_state.q) params.push("q=" + encodeURIComponent(_state.q));
    if (_state.notice_type) params.push("notice_type=" + encodeURIComponent(_state.notice_type));
    if (_state.status) params.push("status=" + encodeURIComponent(_state.status));
    get(BASE + "?" + params.join("&")).then(function (data) {
      var items = data.items || [];
      if (!items.length) {
        el.innerHTML = '<div class="auth-empty" style="padding:20px;"><div class="auth-empty-text">No notices found</div><p class="muted">Upload a date-sheet PDF to get started.</p></div>';
        return;
      }
      var h = '<div style="overflow-x:auto;"><table class="admin-table" style="width:100%;border-collapse:collapse;">';
      h += "<thead><tr><th>Title</th><th>Type</th><th>Programmes</th><th>Status</th><th>Updated</th><th></th></tr></thead><tbody>";
      items.forEach(function (n) {
        h += "<tr style='border-bottom:1px solid var(--line);'>";
        h += "<td style='padding:10px 12px;'><b>" + esc(n.title) + "</b>" +
          (n.exam_session_label ? '<div class="muted" style="font-size:12px;">' + esc(n.exam_session_label) + "</div>" : "") +
          (n.extraction_error ? '<div style="font-size:12px;color:#dc2626;">' + esc(n.extraction_error) + "</div>" : "") +
          "</td>";
        h += "<td style='padding:10px 12px;white-space:nowrap;'>" + esc(n.notice_type) + "</td>";
        h += "<td style='padding:10px 12px;white-space:nowrap;'>" + esc((n.programme_ids || []).join(", ")) + "</td>";
        h += "<td style='padding:10px 12px;white-space:nowrap;'>" + statusBadge(n) + "</td>";
        h += "<td style='padding:10px 12px;white-space:nowrap;' class='muted'>" + esc((n.updated_at || n.created_at || "").slice(0, 10)) + "</td>";
        h += '<td style="padding:10px 12px;white-space:nowrap;"><button class="btn sm ghost" data-open="' + n.id + '">Manage</button></td>';
        h += "</tr>";
      });
      h += "</tbody></table></div>";
      h += '<div class="muted" style="margin-top:10px;font-size:13px;">' + data.total + " notice(s)</div>";
      el.innerHTML = h;
      el.querySelectorAll("[data-open]").forEach(function (btn) {
        btn.addEventListener("click", function () { openDetail(btn.getAttribute("data-open")); });
      });
    }).catch(function (err) {
      el.innerHTML = '<div class="auth-empty" style="padding:20px;"><div class="auth-empty-text">Could not load notices</div><p class="muted">' + esc(err.message) + "</p></div>";
    });
  }

  function uploadNotice() {
    var fileEl = document.getElementById("nt_file");
    var statusEl = document.getElementById("nt_uploadStatus");
    var file = fileEl && fileEl.files && fileEl.files[0];
    if (!file) { statusEl.textContent = "Choose a PDF/DOCX file first."; return; }
    statusEl.textContent = "Uploading...";
    var fd = new FormData();
    fd.append("file", file);
    var title = document.getElementById("nt_title").value.trim();
    if (title) fd.append("title", title);
    fd.append("notice_type", document.getElementById("nt_type").value || "date_sheet");
    var progs = document.getElementById("nt_progs").value.trim();
    if (progs) fd.append("programme_ids", progs);
    var cats = document.getElementById("nt_cats").value.trim();
    if (cats) fd.append("categories", cats);
    post(BASE, fd, true).then(function (n) {
      statusEl.textContent = "Uploaded. Review schedule entries, then Verify and Publish.";
      fileEl.value = "";
      openDetail(n.id);
    }).catch(function (err) {
      statusEl.textContent = "";
      toast(err.message, "error");
    });
  }

  /* ------------------------------------------------------------------ */
  /* Detail                                                              */
  /* ------------------------------------------------------------------ */

  function openDetail(id) {
    _detailId = id;
    var h = "";
    h += '<button class="btn sm ghost" id="nt_back" style="margin-bottom:12px;">&larr; Back to notices</button>';
    h += '<div id="nt_detail" style="min-height:120px;"><div class="auth-loading">Loading notice...</div></div>';
    root().innerHTML = h;
    document.getElementById("nt_back").addEventListener("click", renderList);
    loadDetail(id);
  }

  function loadDetail(id) {
    get(BASE + "/" + id).then(function (n) {
      renderDetail(n);
    }).catch(function (err) {
      document.getElementById("nt_detail").innerHTML =
        '<div class="auth-empty" style="padding:20px;"><div class="auth-empty-text">Could not load notice</div><p class="muted">' + esc(err.message) + "</p></div>";
    });
  }

  function renderDetail(n) {
    var el = document.getElementById("nt_detail");
    var h = "";
    h += '<div class="admin-card" style="margin-bottom:16px;">';
    h += '<div style="display:flex;justify-content:space-between;flex-wrap:wrap;gap:10px;align-items:flex-start;">';
    h += "<div><h3 style='margin:0 0 6px 0;'>" + esc(n.title) + "</h3>" + statusBadge(n) + "</div>";
    h += '<div style="display:flex;gap:8px;flex-wrap:wrap;">';
    if (n.extraction_status === "extraction_failed" || n.extraction_status === "draft") {
      h += '<button class="btn sm ghost" id="nt_reExtract">Re-Extract</button>';
    }
    if (!n.is_verified) {
      h += '<button class="btn sm green" id="nt_verify">Verify</button>';
    } else {
      h += '<button class="btn sm ghost" id="nt_unverify" style="opacity:.7;font-size:12px;" title="Mark back to pending (after edits)">Re-verify after edits</button>';
      if (!n.is_published) {
        h += '<button class="btn green" id="nt_publish">Publish</button>';
      } else {
        h += '<button class="btn sm ghost" id="nt_unpublish">Unpublish</button>';
      }
    }
    h += '<button class="btn sm ghost" id="nt_viewFile">View File</button>';
    h += '<button class="btn sm ghost danger" id="nt_delete">Delete</button>';
    h += "</div></div>";
    h += '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:8px;margin-top:14px;">';
    h += metaField("Title", "n_meta_title", n.title);
    h += metaSelect("Type", "n_meta_type", n.notice_type, ["date_sheet", "notice"]);
    h += metaField("Programmes (JSON array)", "n_meta_progs", JSON.stringify(n.programme_ids || []));
    h += metaField("Categories (JSON array)", "n_meta_cats", JSON.stringify(n.categories || []));
    h += "</div>";
    h += '<div style="margin-top:10px;"><button class="btn sm green" id="nt_saveMeta">Save Metadata</button> <span class="muted" id="nt_metaMsg" style="font-size:13px;"></span></div>';
    h += "</div>";

    h += '<div class="admin-card" style="margin-bottom:16px;">';
    h += '<h3 style="margin:0 0 10px 0;">Schedule Entries</h3>';
    h += '<div id="nt_entries"><div class="auth-loading">Loading schedule entries...</div></div>';
    h += "</div>";

    h += '<div class="admin-card">';
    h += '<h3 style="margin:0 0 10px 0;">Add Schedule Entry (manual)</h3>';
    h += '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:8px;">';
    h += manualInput("Programme", "n_add_programme_id", "");
    h += manualInput("Programme name", "n_add_programme_name", "");
    h += manualInput("Stream", "n_add_stream", "");
    h += manualInput("Semester", "n_add_semester", "");
    h += manualInput("Batch", "n_add_batch", "");
    h += manualInput("Exam type", "n_add_exam_type", "");
    h += manualInput("Exam date (YYYY-MM-DD)", "n_add_exam_date", "");
    h += manualInput("Day", "n_add_day", "");
    h += manualInput("Start (HH:MM)", "n_add_start_time", "");
    h += manualInput("End (HH:MM)", "n_add_end_time", "");
    h += manualInput("Subject code", "n_add_subject_code", "");
    h += manualInput("Subject", "n_add_subject", "");
    h += manualInput("Paper code", "n_add_paper_code", "");
    h += manualInput("Venue", "n_add_venue", "");
    h += "</div>";
    h += '<div style="margin-top:10px;"><button class="btn sm green" id="nt_addEntry">Add Entry</button> <span class="muted" id="nt_addMsg" style="font-size:13px;"></span></div>';
    h += '<p class="muted" style="font-size:12px;margin-top:8px;">"Re-verify after edits" re-applies the structural gate: if all rows are valid it refreshes the verified marker; if any row is broken the notice is auto-downgraded and unpublished.</p>';
    h += "</div>";

    el.innerHTML = h;

    document.getElementById("nt_saveMeta").addEventListener("click", function () { saveMeta(n.id); });
    document.getElementById("nt_viewFile").addEventListener("click", function () { openFile(n.id); });
    document.getElementById("nt_delete").addEventListener("click", function () {
      if (!confirm("Delete this notice (soft)? It will be hidden from the public chatbot.")) return;
      del(BASE + "/" + n.id).then(function () { toast("Notice deleted", "info"); renderList(); }).catch(function (e) { toast(e.message, "error"); });
    });
    var verifyBtn = document.getElementById("nt_verify");
    if (verifyBtn) verifyBtn.addEventListener("click", function () { verify(n.id); });
    var unverify = document.getElementById("nt_unverify");
    if (unverify) unverify.addEventListener("click", function () { reExtract(n.id); });
    var pubBtn = document.getElementById("nt_publish");
    if (pubBtn) pubBtn.addEventListener("click", function () { publish(n.id); });
    var unpubBtn = document.getElementById("nt_unpublish");
    if (unpubBtn) unpubBtn.addEventListener("click", function () { unpublish(n.id); });
    var reBtn = document.getElementById("nt_reExtract");
    if (reBtn) reBtn.addEventListener("click", function () { reExtract(n.id); });
    document.getElementById("nt_addEntry").addEventListener("click", function () { addEntry(n.id); });

    loadEntries(n.id);
  }

  function metaField(label, id, value) {
    return '<label style="font-size:12px;color:var(--muted);">' + esc(label) +
      '<input id="' + id + '" type="text" style="padding:7px 10px;border:1px solid var(--border);border-radius:6px;font-size:13px;width:100%;" value="' + esc(value) + '"></label>';
  }
  function metaSelect(label, id, value, choices) {
    var o = '<label style="font-size:12px;color:var(--muted);">' + esc(label) + '<select id="' + id + '" style="padding:7px 10px;border:1px solid var(--border);border-radius:6px;font-size:13px;width:100%;margin-top:4px;">';
    choices.forEach(function (c) { o += '<option value="' + esc(c) + '"' + (String(value) === c ? " selected" : "") + ">" + esc(c) + "</option>"; });
    o += "</select></label>";
    return o;
  }
  function manualInput(label, id, value) {
    return '<label style="font-size:12px;color:var(--muted);">' + esc(label) +
      '<input id="' + id + '" type="text" style="padding:7px 10px;border:1px solid var(--border);border-radius:6px;font-size:13px;width:100%;" value="' + esc(value) + '"></label>';
  }

  function saveMeta(id) {
    var body = {
      title: document.getElementById("n_meta_title").value.trim(),
      notice_type: document.getElementById("n_meta_type").value,
    };
    var progs = parseJsonArray(document.getElementById("n_meta_progs").value);
    var cats = parseJsonArray(document.getElementById("n_meta_cats").value);
    if (progs === false || cats === false) {
      document.getElementById("nt_metaMsg").textContent = "Programmes / categories must be valid JSON arrays.";
      return;
    }
    body.programme_ids = progs;
    body.categories = cats;
    document.getElementById("nt_metaMsg").textContent = "Saving...";
    patch(BASE + "/" + id, body).then(function () {
      document.getElementById("nt_metaMsg").textContent = "Saved.";
      setTimeout(function () { loadDetail(id); }, 600);
    }).catch(function (e) { document.getElementById("nt_metaMsg").textContent = e.message; });
  }

  function parseJsonArray(raw) {
    var v = (raw || "").trim();
    if (!v) return [];
    try {
      var arr = JSON.parse(v);
      if (!Array.isArray(arr)) return false;
      return arr.map(function (x) { return String(x); });
    } catch (e) { return false; }
  }

  function openFile(id) { window.open(BASE + "/" + id + "/file", "_blank"); }

  function verify(id) {
    post(BASE + "/" + id + "/verify").then(function () { toast("Notice verified", "info"); loadDetail(id); })
      .catch(function (e) { toast("Verify failed: " + e.message, "error"); });
  }
  function publish(id) {
    post(BASE + "/" + id + "/publish").then(function () { toast("Notice published", "info"); loadDetail(id); })
      .catch(function (e) { toast("Publish failed: " + e.message, "error"); });
  }
  function unpublish(id) {
    post(BASE + "/" + id + "/unpublish").then(function () { toast("Notice unpublished", "info"); loadDetail(id); })
      .catch(function (e) { toast("Unpublish failed: " + e.message, "error"); });
  }
  function reExtract(id) {
    post(BASE + "/" + id + "/extract").then(function () { toast("Extraction re-run (any broken edits auto-unpublish the notice)", "info"); loadDetail(id); })
      .catch(function (e) { toast(e.message, "error"); });
  }

  /* ------------------------------------------------------------------ */
  /* Entries                                                             */
  /* ------------------------------------------------------------------ */

  function loadEntries(id) {
    get(BASE + "/" + id + "/schedule").then(function (data) {
      renderEntries(id, data.entries || []);
    }).catch(function (err) {
      document.getElementById("nt_entries").innerHTML = '<div class="auth-empty" style="padding:10px;"><p class="muted">' + esc(err.message) + "</p></div>";
    });
  }

  function renderEntries(id, entries) {
    var el = document.getElementById("nt_entries");
    if (!entries.length) {
      el.innerHTML = '<div class="auth-empty" style="padding:12px;"><p class="muted">No schedule rows yet. Use “Add Schedule Entry” or upload a date-sheet PDF and re-extract.</p></div>';
      return;
    }
    var cols = ["exam_date", "day", "start_time", "end_time", "subject", "paper_code", "venue", "programme_id", "semester", "stream", "batch"];
    var h = '<div style="overflow-x:auto;"><table class="admin-table" style="width:100%;border-collapse:collapse;font-size:13px;">';
    h += "<thead><tr><th>Row</th>";
    cols.forEach(function (c) { h += "<th>" + esc(c) + "</th>"; });
    h += "<th>Status</th><th></th></tr></thead><tbody>";
    entries.forEach(function (e) {
      h += "<tr style='border-bottom:1px solid var(--line);' data-entry='" + e.id + "'>";
      h += "<td style='padding:6px 8px;'>" + e.row_no + "</td>";
      cols.forEach(function (c) {
        h += '<td style="padding:6px 8px;"><input type="text" class="nt-row" data-id="' + e.id + '" data-field="' + esc(c) +
          '" value="' + esc(e[c] === null || e[c] === undefined ? "" : e[c]) + '" style="padding:5px 8px;border:1px solid var(--border);border-radius:6px;font-size:12.5px;width:100%;min-width:90px;"></td>';
      });
      h += "<td style='padding:6px 8px;white-space:nowrap;font-size:12px;color:var(--muted);'>" + esc(e.extraction_status || "") + (e.is_corrected ? " · corrected" : "") + "</td>";
      h += '<td style="padding:6px 8px;white-space:nowrap;">' +
        '<button class="btn sm ghost nt-save" data-id="' + e.id + '">Save</button> ' +
        '<button class="btn sm ghost nt-del" data-id="' + e.id + '">&times;</button></td>';
      h += "</tr>";
    });
    h += "</tbody></table></div>";
    el.innerHTML = h;
    el.querySelectorAll(".nt-save").forEach(function (btn) {
      btn.addEventListener("click", function () { saveEntry(id, btn.getAttribute("data-id")); });
    });
    el.querySelectorAll(".nt-del").forEach(function (btn) {
      btn.addEventListener("click", function () { deleteEntry(id, btn.getAttribute("data-id")); });
    });
  }

  function saveEntry(noticeId, entryId) {
    var fields = ["exam_date", "day", "start_time", "end_time", "subject", "paper_code", "venue", "programme_id", "programme_name", "subject_code", "semester", "stream", "batch", "exam_type"];
    var body = {};
    fields.forEach(function (f) {
      var el = document.querySelector('.nt-row[data-id="' + entryId + '"][data-field="' + f + '"]');
      if (el) body[f] = el.value.trim();
    });
    patch(BASE + "/" + noticeId + "/schedule/" + entryId, body)
      .then(function () { toast("Entry saved (notice re-validated)", "info"); loadEntries(noticeId); })
      .catch(function (e) { toast(e.message, "error"); });
  }

  function deleteEntry(noticeId, entryId) {
    if (!confirm("Delete this schedule row?")) return;
    del(BASE + "/" + noticeId + "/schedule/" + entryId)
      .then(function () { toast("Entry deleted (notice re-validated)", "info"); loadEntries(noticeId); })
      .catch(function (e) { toast(e.message, "error"); });
  }

  function addEntry(id) {
    var body = {};
    var ids = ["programme_id", "programme_name", "stream", "semester", "batch", "exam_type", "exam_date", "day", "start_time", "end_time", "subject_code", "subject", "paper_code", "venue"];
    ids.forEach(function (f) {
      var v = document.getElementById("n_add_" + f).value.trim();
      if (v) body[f] = v;
    });
    document.getElementById("nt_addMsg").textContent = "Adding...";
    post(BASE + "/" + id + "/schedule", body)
      .then(function () { document.getElementById("nt_addMsg").textContent = "Added."; loadEntries(id); })
      .catch(function (e) { document.getElementById("nt_addMsg").textContent = e.message; });
  }

  /* ------------------------------------------------------------------ */

  window.CUS = window.CUS || {};
  window.CUS.noticesAdminInit = renderList;
})();