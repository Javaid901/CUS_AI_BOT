(function () {
  "use strict";

  if (!window.CUS_API_BASE) throw new Error("CUS_API_BASE not defined");
  var API = window.CUS_API_BASE;
  var BASE = API + "/api/admin/exam-forms";

  function authHeaders() {
    var h = {};
    var t = localStorage.getItem("cus_admin_token");
    if (t) h.Authorization = "Bearer " + t;
    return h;
  }
  function log(m) { console.log("[CUS-ExamForms] " + m); }

  function req(method, url, body) {
    var opts = { method: method, headers: authHeaders() };
    if (body !== undefined) opts.body = JSON.stringify(body);
    if (opts.body) opts.headers["Content-Type"] = "application/json";
    return fetch(url, opts).then(function (r) {
      if (r.status === 401) { log("Unauthorized, reloading"); window.location.reload(); throw new Error("Unauthorized"); }
      return r.json().then(function (d) {
        if (!r.ok) throw new Error((d && d.error && d.error.message) || (d && d.detail) || ("HTTP " + r.status));
        return d;
      });
    });
  }
  function get(url) { return req("GET", url); }
  function post(url, body) { return req("POST", url, body); }
  function patch(url, body) { return req("PATCH", url, body); }
  function del(url, body) { return req("DELETE", url, body); }

  function upload(url, formData) {
    var opts = { method: "POST", headers: authHeaders(), body: formData };
    return fetch(url, opts).then(function (r) {
      if (r.status === 401) { window.location.reload(); throw new Error("Unauthorized"); }
      return r.json().then(function (d) {
        if (!r.ok) throw new Error((d && d.error && d.error.message) || (d && d.detail) || ("HTTP " + r.status));
        return d;
      });
    });
  }

  var esc = function (s) {
    return String(s === undefined || s === null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  };

  var _page = 1;
  var _q = "";
  var _sem = "";
  var _status = "";
  var _importFilename = "";
  var _importRawRows = [];

  var _SEM_OPTIONS = "";
  for (var i = 1; i <= 8; i++) {
    _SEM_OPTIONS += '<option value="' + i + '">Semester ' + i + "</option>";
  }

  var _STATUS_OPTIONS = ['<option value="">All statuses</option>'];
  ["Pending", "Submitted", "Approved", "Rejected", "Withdrawn"].forEach(function (s) {
    _STATUS_OPTIONS.push('<option value="' + s + '">' + s + "</option>");
  });

  function root() { return document.getElementById("stPaneExamForms"); }
  function toast(msg, type) { if (window.CUS_TOAST) window.CUS_TOAST(msg, type || "success"); }

  // ========== Render ==========
  function render() {
    if (!root()) return;
    root().innerHTML =
      '<div class="admin-card">' +
      "<h2>Student Exam Forms</h2>" +
      '<p class="sub">Exam forms are structured records (semester, exam type, subjects, status, fee). Students fill and submit ' +
      "their own forms via Student Services; Super Admins provision, update payment details and set the lifecycle status here. " +
      "No file upload — import CSV/XLSX with an explicit preview; duplicates block the import.</p>" +
      "</div>" +
      '<div class="admin-card">' +
      '<div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px;margin-bottom:16px;">' +
      '<div class="kpi kpi-sm" style="flex:1;min-width:160px;"><div class="box"><div class="n" id="efTotal">-</div>' +
      '<div class="l">Exam forms</div></div></div>' +
      '<div style="display:flex;gap:8px;flex-wrap:wrap;">' +
      '<input id="efSearch" type="search" class="st-input" placeholder="Search reg no / name" style="min-width:200px;">' +
      '<select id="efSem" class="st-input"><option value="">All semesters</option>' +
      _SEM_OPTIONS +
      "</select>" +
      '<select id="efStatus" class="st-input">' + _STATUS_OPTIONS.join("") + "</select>" +
      '<button class="btn" id="efAdd">+ Add Form</button>' +
      '<button class="btn green" id="efImport">&#8593; Import (CSV / XLSX)</button>' +
      "</div></div>" +
      '<div style="overflow-x:auto;"><table class="admin-table" style="width:100%;border-collapse:collapse;">' +
      "<thead><tr><th>Reg No</th><th>Name</th><th>Sem</th><th>Exam</th><th>Year</th><th>Status</th>" +
      "<th>Fee</th><th>Amount</th><th>Txn</th><th>Submitted</th><th>Subjects</th><th></th></tr></thead>" +
      '<tbody id="efRows"><tr><td colspan="12" style="text-align:center;color:var(--muted);">Loading&hellip;</td></tr></tbody>' +
      "</table></div>" +
      '<div style="display:flex;justify-content:space-between;align-items:center;margin-top:14px;">' +
      '<span id="efPageInfo" style="color:var(--muted);font-size:13px;"></span>' +
      '<span style="display:flex;gap:8px;"><button class="btn sm ghost" id="efPrev">&#8592; Prev</button>' +
      '<button class="btn sm ghost" id="efNext">Next &#8594;</button></span>' +
      "</div></div>";

    document.getElementById("efAdd").addEventListener("click", function () { openForm(null); });
    document.getElementById("efImport").addEventListener("click", openImport);
    document.getElementById("efSem").addEventListener("change", function () {
      _sem = this.value;
      _page = 1;
      load();
    });
    document.getElementById("efStatus").addEventListener("change", function () {
      _status = this.value;
      _page = 1;
      load();
    });
    document.getElementById("efSearch").addEventListener("input", function () {
      var v = this.value.trim();
      if (v === _q) return;
      _q = v;
      _page = 1;
      load();
    });
    document.getElementById("efPrev").addEventListener("click", function () {
      if (_page > 1) { _page -= 1; load(); }
    });
    document.getElementById("efNext").addEventListener("click", function () {
      _page += 1; load();
    });
    load();
  }

  function load() {
    var url = BASE + "?q=" + encodeURIComponent(_q) + (_sem ? "&semester=" + encodeURIComponent(_sem) : "") +
      "&form_status=" + encodeURIComponent(_status) + "&page=" + _page + "&page_size=50";
    get(url).then(function (d) {
      var t = document.getElementById("efTotal");
      if (t) t.textContent = d.total;
      var rows = document.getElementById("efRows");
      var html = "";
      (d.exam_forms || []).forEach(function (r) {
        var n = (r.subjects || []).length;
        var statusColor = r.form_status === "Approved" ? "#15803d" : (r.form_status === "Rejected" || r.form_status === "Withdrawn" ? "#dc2626" : "var(--navy)");
        html += "<tr>" +
          "<td><b>" + esc(r.reg_no) + "</b></td>" +
          "<td>" + esc(r.name) + "</td>" +
          "<td>" + esc(r.semester) + "</td>" +
          "<td>" + esc(r.exam_type || "Regular") + "</td>" +
          "<td>" + esc(r.academic_year || "-") + "</td>" +
          "<td><span style='color:" + statusColor + ";font-weight:700;'>" + esc(r.form_status || "Pending") + "</span></td>" +
          "<td>" + esc(r.fee_status || "-") + "</td>" +
          "<td>" + (r.fee_amount === null || r.fee_amount === undefined ? "-" : esc(r.fee_amount)) + "</td>" +
          "<td>" + esc(r.transaction_id || "-") + "</td>" +
          "<td>" + esc(r.submission_date || "-") + "</td>" +
          "<td>" + (n ? esc(n) + " subject" + (n === 1 ? "" : "s") : "-") + "</td>" +
          '<td style="text-align:right;white-space:nowrap;">' +
          '<button class="btn sm ghost" data-act="edit" data-id="' + esc(r.id) + '">Edit</button> ' +
          '<button class="btn sm ghost" data-act="status" data-id="' + esc(r.id) + '">Status</button> ' +
          '<button class="btn sm danger" data-act="del" data-id="' + esc(r.id) + '">Withdraw</button>' +
          "</td></tr>";
      });
      if (!html) html = '<tr><td colspan="12" style="text-align:center;color:var(--muted);">No exam forms found.</td></tr>';
      rows.innerHTML = html;
      rows.querySelectorAll("[data-act]").forEach(function (b) {
        b.addEventListener("click", function () {
          var id = b.getAttribute("data-id");
          var act = b.getAttribute("data-act");
          if (act === "edit") openForm(id);
          else if (act === "status") openStatus(id);
          else delCard(id);
        });
      });
      var pi = document.getElementById("efPageInfo");
      if (pi) {
        var pages = Math.max(1, Math.ceil(d.total / d.page_size));
        pi.textContent = "Page " + d.page + " of " + pages + " (" + d.total + " total)";
        document.getElementById("efNext").disabled = d.page >= pages;
        document.getElementById("efPrev").disabled = d.page <= 1;
      }
    }).catch(function (e) { toast(e.message, "error"); });
  }

  // ========== Create / Edit (single form) ==========
  function openForm(id) {
    var editing = null;
    function finalize() {
      var title = editing ? "Edit Exam Form" : "Add Exam Form";
      var regField = editing ? "" :
        '<label style="margin-bottom:8px;"><b style="font-size:13px;">Registration Number <span style="color:#dc2626;">*</span></b>' +
        '<input id="ef_reg_no" type="text" class="st-input" style="width:100%;" placeholder="e.g. CUS-PR-A0001" required></label>';
      var subjects = editing ? (editing.subjects || []).join("\n") : "";
      var semSelect = '<select id="ef_semester" class="st-input" style="width:100%;">' + _SEM_OPTIONS + "</select>";
      if (editing) {
        semSelect = semSelect.replace('<option value="' + editing.semester + '">', '<option value="' + editing.semester + '" selected>');
      }
      var typeSelect = '<select id="ef_exam_type" class="st-input" style="width:100%;">' +
        '<option value="Regular">Regular</option><option value="Backlog">Backlog</option></select>';
      if (editing && editing.exam_type) {
        typeSelect = typeSelect.replace('<option value="' + editing.exam_type + '">', '<option value="' + editing.exam_type + '" selected>');
      }
      var feeSelect = '<select id="ef_fee_status" class="st-input" style="width:100%;">' +
        '<option value="Unpaid">Unpaid</option><option value="Paid">Paid</option></select>';
      if (editing && editing.fee_status) {
        feeSelect = feeSelect.replace('<option value="' + editing.fee_status + '">', '<option value="' + editing.fee_status + '" selected>');
      }
      var body =
        '<label style="margin-bottom:8px;"><b style="font-size:13px;">Semester <span style="color:#dc2626;">*</span></b>' + semSelect + "</label>" +
        '<div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;">' +
        '<label style="margin-bottom:8px;"><b style="font-size:13px;">Exam Type</b>' + typeSelect + "</label>" +
        '<label style="margin-bottom:8px;"><b style="font-size:13px;">Academic Year</b>' +
        '<input id="ef_academic_year" type="text" class="st-input" style="width:100%;" placeholder="e.g. 2024-2025" value="' + esc((editing && editing.academic_year) || "") + '"></label>' +
        "</div>" + regField +
        '<div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;">' +
        '<label style="margin-bottom:8px;"><b style="font-size:13px;">Fee Status</b>' + feeSelect + "</label>" +
        '<label style="margin-bottom:8px;"><b style="font-size:13px;">Fee Amount</b>' +
        '<input id="ef_fee_amount" type="number" min="0" step="1" class="st-input" style="width:100%;" value="' + esc((editing && editing.fee_amount !== null && editing.fee_amount !== undefined) ? editing.fee_amount : "") + '"></label>' +
        "</div>" +
        '<div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;">' +
        '<label style="margin-bottom:8px;"><b style="font-size:13px;">Transaction Id</b>' +
        '<input id="ef_transaction_id" type="text" class="st-input" style="width:100%;" value="' + esc((editing && editing.transaction_id) || "") + '"></label>' +
        '<label style="margin-bottom:8px;"><b style="font-size:13px;">Submission Date</b>' +
        '<input id="ef_submission_date" type="text" class="st-input" style="width:100%;" placeholder="e.g. 15-May-2025" value="' + esc((editing && editing.submission_date) || "") + '"></label>' +
        "</div>" +
        '<label style="margin-bottom:8px;"><b style="font-size:13px;">Subjects</b><span style="font-size:12px;color:var(--muted);"> one per line</span>' +
        '<textarea id="ef_subjects" class="st-input" style="width:100%;" rows="4">' + esc(subjects) + "</textarea></label>";
      var ov = document.createElement("div");
      ov.className = "modal-overlay";
      ov.style.display = "flex";
      ov.innerHTML =
        '<div class="modal-box">' +
        '<div class="modal-header"><h3>' + esc(title) + '</h3><button type="button" class="modal-close" data-mclose="1">&times;</button></div>' +
        '<div class="modal-body" style="padding:20px 24px;overflow-y:auto;max-height:70vh;">' + body +
        '<div id="efFormError" style="display:none;color:#dc2626;margin-top:10px;font-size:13px;"></div></div>' +
        '<div class="modal-footer"><button type="button" class="btn ghost" data-mclose="1">Cancel</button>' +
        '<button type="submit" class="btn green" id="efSave">' + esc(editing ? "Save Changes" : "Add Form") + "</button></div>" +
        "</div>";
      document.body.appendChild(ov);
      ov.querySelectorAll("[data-mclose]").forEach(function (b) {
        b.addEventListener("click", function () { ov.remove(); });
      });
      ov.addEventListener("click", function (e) { if (e.target === ov) ov.remove(); });
      var form = document.createElement("form");
      form.style.display = "none";
      ov.appendChild(form);
      var saveBtn = ov.querySelector("#efSave");
      saveBtn.addEventListener("click", function () {
        var errEl = ov.querySelector("#efFormError");
        errEl.style.display = "none";
        var subjects = ov.querySelector("#ef_subjects").value.split(/\r?\n/).map(function (x) { return x.trim(); }).filter(Boolean);
        var payload = {
          semester: parseInt(ov.querySelector("#ef_semester").value, 10),
          exam_type: ov.querySelector("#ef_exam_type").value.trim() || "Regular",
          academic_year: ov.querySelector("#ef_academic_year").value.trim(),
          fee_status: ov.querySelector("#ef_fee_status").value.trim() || "Unpaid",
          transaction_id: ov.querySelector("#ef_transaction_id").value.trim(),
          submission_date: ov.querySelector("#ef_submission_date").value.trim(),
          subjects: subjects
        };
        var amt = ov.querySelector("#ef_fee_amount").value.trim();
        if (amt !== "") payload.fee_amount = parseInt(amt, 10);
        if (!editing) payload.reg_no = ov.querySelector("#ef_reg_no").value.trim();
        if (!payload.reg_no && !editing) { errEl.textContent = "Registration number is required."; errEl.style.display = "block"; return; }
        saveBtn.disabled = true;
        var p = editing ? patch(BASE + "/" + encodeURIComponent(editing.id), payload)
                        : post(BASE, payload);
        p.then(function (res) {
          ov.remove();
          toast(res.message || "Exam form " + (editing ? "updated" : "added"), "success");
          load();
        }).catch(function (e) {
          errEl.textContent = e.message;
          errEl.style.display = "block";
          saveBtn.disabled = false;
        });
      });
    }
    if (id) {
      get(BASE + "?" + (new URLSearchParams({ q: _q, semester: _sem, form_status: _status, page: _page, page_size: 100 })).toString()).then(function (d) {
        var found = (d.exam_forms || []).filter(function (c) { return String(c.id) === String(id); })[0];
        if (found) { editing = found; finalize(); }
        else { toast("That exam form no longer exists — refreshing.", "error"); _page = 1; load(); }
      }).catch(function (e) { toast(e.message, "error"); });
      return;
    }
    finalize();
  }

  // ========== Status transition ==========
  function openStatus(id) {
    get(BASE + "?" + (new URLSearchParams({ q: _q, semester: _sem, form_status: _status, page: _page, page_size: 100 })).toString()).then(function (d) {
      var found = (d.exam_forms || []).filter(function (c) { return String(c.id) === String(id); })[0];
      if (!found) { toast("That exam form no longer exists.", "error"); return; }
      var opts = "";
      ["Pending", "Submitted", "Approved", "Rejected", "Withdrawn"].forEach(function (s) {
        opts += '<option value="' + s + '"' + (found.form_status === s ? " selected" : "") + ">" + s + "</option>";
      });
      var ov = document.createElement("div");
      ov.className = "modal-overlay";
      ov.style.display = "flex";
      ov.innerHTML =
        '<div class="modal-box">' +
        '<div class="modal-header"><h3>Exam Form Status</h3><button type="button" class="modal-close" data-mclose="1">&times;</button></div>' +
        '<div class="modal-body" style="padding:20px 24px;">' +
        "<p style='margin:0 0 12px;font-size:14px;'>Set the lifecycle status for <b>" + esc(found.reg_no) + "</b> " +
        "(" + esc(found.exam_type) + " · Semester " + esc(found.semester) + "). Current status: <b>" + esc(found.form_status) + "</b>.</p>" +
        '<select id="ef_status_sel" class="st-input" style="width:100%;">' + opts + "</select>" +
        '<div id="efStatusError" style="display:none;color:#dc2626;margin-top:10px;font-size:13px;"></div>' +
        "</div>" +
        '<div class="modal-footer"><button type="button" class="btn ghost" data-mclose="1">Cancel</button>' +
        '<button class="btn green" id="efStatusSave">Update Status</button></div>' +
        "</div>";
      document.body.appendChild(ov);
      ov.querySelectorAll("[data-mclose]").forEach(function (b) {
        b.addEventListener("click", function () { ov.remove(); });
      });
      ov.addEventListener("click", function (e) { if (e.target === ov) ov.remove(); });
      ov.querySelector("#efStatusSave").addEventListener("click", function () {
        var errEl = ov.querySelector("#efStatusError");
        errEl.style.display = "none";
        var status = ov.querySelector("#ef_status_sel").value;
        post(BASE + "/" + encodeURIComponent(id) + "/status", { form_status: status }).then(function () {
          ov.remove();
          toast("Status updated to " + status, "success");
          load();
        }).catch(function (e) {
          errEl.textContent = e.message;
          errEl.style.display = "block";
        });
      });
    }).catch(function (e) { toast(e.message, "error"); });
  }

  function delCard(id) {
    if (!confirm("Withdraw this exam form? It will no longer be visible to the student.")) return;
    del(BASE + "/" + encodeURIComponent(id)).then(function () {
      toast("Exam form withdrawn", "success");
      load();
    }).catch(function (e) { toast(e.message, "error"); });
  }

  // ========== Import ==========
  function openImport() {
    _importFilename = "";
    _importRawRows = [];
    var ov = document.createElement("div");
    ov.className = "modal-overlay";
    ov.style.display = "flex";
    ov.id = "efImportModal";
    ov.innerHTML =
      '<div class="modal-box" style="max-width:880px;">' +
      '<div class="modal-header"><h3>Import Exam Forms</h3><button type="button" class="modal-close" data-mclose="1">&times;</button></div>' +
      '<div id="efImportBody" style="padding:18px 24px;max-height:65vh;overflow-y:auto;"></div>' +
      '<div class="modal-footer"><button type="button" class="btn ghost" data-mclose="1">Close</button></div>' +
      "</div>";
    document.body.appendChild(ov);
    ov.querySelectorAll("[data-mclose]").forEach(function (b) {
      b.addEventListener("click", function () { ov.remove(); });
    });
    ov.addEventListener("click", function (e) { if (e.target === ov) ov.remove(); });
    _importStageFile(ov);
  }

  function _importStageFile(ov) {
    var body = document.getElementById("efImportBody");
    body.innerHTML =
      '<p style="margin:0 0 10px;">Choose a <b>CSV</b> or <b>XLSX</b> file. Required columns: <b>Registration Number</b>,' +
      ' <b>Semester</b>, <b>Exam Type</b>. Optional: Academic Year, Subjects, Form Status, Fee Status, Fee Amount,' +
      " Transaction Id, Submission Date.</p>" +
      '<p style="color:var(--muted);font-size:13px;margin:0 0 12px;">Subjects are one entry per line (or a JSON array). ' +
      "Preview never writes anything. Confirming applies all rows in a single transaction — a duplicate or invalid row rejects the whole import.</p>" +
      '<input type="file" id="efFile" accept=".csv,.xlsx,text/csv,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" style="margin-bottom:14px;">' +
      '<div id="efFileErr" style="display:none;color:#dc2626;font-size:13px;margin-bottom:10px;"></div>' +
      '<div style="display:flex;gap:8px;"><button class="btn green" id="efAnalyze">Analyze file</button>' +
      '<button type="button" class="btn ghost" data-mclose="1">Cancel</button></div>';
    document.getElementById("efAnalyze").addEventListener("click", function () {
      var err = document.getElementById("efFileErr");
      var input = document.getElementById("efFile");
      err.style.display = "none";
      if (!input.files || !input.files.length) {
        err.textContent = "Choose a file first.";
        err.style.display = "block";
        return;
      }
      var file = input.files[0];
      if (!/\.(csv|xlsx)$/i.test(file.name)) {
        err.textContent = "Only CSV or XLSX files are supported.";
        err.style.display = "block";
        return;
      }
      var fd = new FormData();
      fd.append("file", file);
      document.getElementById("efAnalyze").disabled = true;
      upload(BASE + "/preview", fd).then(function (d) {
        _importFilename = d.filename || file.name;
        _importRawRows = d.raw_rows || [];
        _importRenderPreview(ov, d);
      }).catch(function (e) {
        snackIfAlive(err, e.message);
        document.getElementById("efAnalyze").disabled = false;
      });
    });
  }

  function snackIfAlive(el, msg) {
    if (el && el.isConnected) { el.textContent = msg; el.style.display = "block"; }
  }

  function _importRenderPreview(ov, d) {
    var body = document.getElementById("efImportBody");
    var stats =
      '<div style="display:flex;gap:14px;flex-wrap:wrap;margin-bottom:12px;">' +
      '<span style="font-size:13px;">Total rows: <b>' + esc(d.total_rows) + "</b></span>" +
      '<span style="font-size:13px;color:#15803d;">Valid: <b>' + esc(d.valid_count) + "</b></span>" +
      '<span style="font-size:13px;color:#dc2626;">Blocked: <b>' + esc(d.error_count) + "</b></span></div>";
    var errHtml = "";
    if (d.errors && d.errors.length) {
      errHtml = '<div style="background:#fef2f2;border:1px solid #fecaca;border-radius:8px;padding:10px 14px;margin-bottom:12px;">' +
        "<strong style=\"font-size:13px;\">These rows block the import (" + esc(d.errors.length) + "):</strong><ul style=\"margin:6px 0 0;padding-left:18px;\">";
      d.errors.slice(0, 40).forEach(function (e) {
        errHtml += "<li style=\"font-size:12px;color:#b91c1c;\">Row " + esc(e.row) + ": " + esc(e.message) + "</li>";
      });
      if (d.errors.length > 40) errHtml += "<li style=\"font-size:12px;color:#b91c1c;\">…and " + esc(d.errors.length - 40) + " more.</li>";
      errHtml += "</ul></div>";
    }
    var validHtml = "";
    if (d.valid_count) {
      validHtml = '<div style="overflow-x:auto;"><table class="admin-table" style="width:100%;border-collapse:collapse;">' +
        "<thead><tr><th>Row</th><th>Reg No</th><th>Sem</th><th>Exam</th><th>Year</th><th>Status</th></tr></thead><tbody>";
      d.rows.slice(0, 60).forEach(function (r) {
        validHtml += "<tr><td>" + esc(r.row) + "</td><td>" + esc(r.registration_number) + "</td><td>" + esc(r.semester) +
          "</td><td>" + esc(r.exam_type) + "</td><td>" + esc(r.academic_year || "-") + "</td><td>" + esc(r.form_status || "-") + "</td></tr>";
      });
      if (d.valid_count > 60) validHtml += '<tr><td colspan="6" style="text-align:center;color:var(--muted);">…and ' + esc(d.valid_count - 60) + " more valid rows.</td></tr>";
      validHtml += "</tbody></table></div>";
    }
    var confirmBtn = "";
    if (d.error_count === 0 && d.valid_count > 0) {
      confirmBtn = '<div style="display:flex;gap:8px;margin-top:14px;">' +
        '<button class="btn green" id="efConfirm">Apply import (' + esc(d.valid_count) + " forms)</button>" +
        '<button type="button" class="btn ghost" data-mclose="1">Cancel</button></div>';
    } else {
      confirmBtn = '<div style="display:flex;gap:8px;margin-top:14px;">' +
        '<button type="button" class="btn ghost" data-mclose="1">Fix file and try again</button></div>';
    }
    body.innerHTML = stats + errHtml + validHtml + confirmBtn;
    var cb = document.getElementById("efConfirm");
    if (cb) cb.addEventListener("click", function () {
      cb.disabled = true;
      post(BASE + "/confirm", { filename: _importFilename, rows: _importRawRows }).then(function (res) {
        ov.remove();
        toast(res.message || "Exam forms imported", "success");
        load();
      }).catch(function (e) {
        var err = document.createElement("div");
        err.style.color = "#dc2626"; err.style.fontSize = "13px"; err.style.marginTop = "10px";
        err.textContent = e.message;
        cb.parentNode.appendChild(err);
        cb.disabled = false;
      });
    });
  }

  window.CUS.examFormsInit = render;
})();