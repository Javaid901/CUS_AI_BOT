(function () {
  "use strict";

  if (!window.CUS_API_BASE) throw new Error("CUS_API_BASE not defined");
  var API = window.CUS_API_BASE;
  var BASE = API + "/api/admin/admit-cards";

  function authHeaders() {
    var h = {};
    var t = localStorage.getItem("cus_admin_token");
    if (t) h.Authorization = "Bearer " + t;
    return h;
  }
  function log(m) { console.log("[CUS-AdmitCards] " + m); }

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
  var _importFilename = "";
  var _importRawRows = [];

  var _SEM_OPTIONS = "";
  for (var i = 1; i <= 8; i++) {
    _SEM_OPTIONS += '<option value="' + i + '">Semester ' + i + "</option>";
  }

  function root() { return document.getElementById("stPaneAdmitCards"); }
  function toast(msg, type) { if (window.CUS_TOAST) window.CUS_TOAST(msg, type || "success"); }

  // ========== Render ==========
  function render() {
    if (!root()) return;
    root().innerHTML =
      '<div class="admin-card">' +
      "<h2>Student Admit Cards</h2>" +
      '<p class="sub">Issued admit cards are shown to students via Student Services. Cards are structured records ' +
      "(centre, session, reporting time, subjects, instructions) — no file upload. Issue cards individually or import CSV/XLSX " +
      "with an explicit preview; duplicate rows block the import.</p>" +
      "</div>" +
      '<div class="admin-card">' +
      '<div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px;margin-bottom:16px;">' +
      '<div class="kpi kpi-sm" style="flex:1;min-width:160px;"><div class="box"><div class="n" id="acTotal">-</div>' +
      '<div class="l">Admit cards</div></div></div>' +
      '<div style="display:flex;gap:8px;flex-wrap:wrap;">' +
      '<input id="acSearch" type="search" class="st-input" placeholder="Search reg no / name" style="min-width:220px;">' +
      '<select id="acSem" class="st-input"><option value="">All semesters</option>' +
      _SEM_OPTIONS +
      '</select>' +
      '<button class="btn" id="acAdd">+ Issue Card</button>' +
      '<button class="btn green" id="acImport">&#8593; Import (CSV / XLSX)</button>' +
      "</div></div>" +
      '<div style="overflow-x:auto;"><table class="admin-table" style="width:100%;border-collapse:collapse;">' +
      "<thead><tr><th>Reg No</th><th>Name</th><th>Sem</th><th>Exam</th><th>Session</th><th>Year</th><th>Centre</th>" +
      "<th>Code</th><th>Reporting</th><th>Subjects</th><th>Issued</th><th></th></tr></thead>" +
      '<tbody id="acRows"><tr><td colspan="12" style="text-align:center;color:var(--muted);">Loading&hellip;</td></tr></tbody>' +
      "</table></div>" +
      '<div style="display:flex;justify-content:space-between;align-items:center;margin-top:14px;">' +
      '<span id="acPageInfo" style="color:var(--muted);font-size:13px;"></span>' +
      '<span style="display:flex;gap:8px;"><button class="btn sm ghost" id="acPrev">&#8592; Prev</button>' +
      '<button class="btn sm ghost" id="acNext">Next &#8594;</button></span>' +
      "</div></div>";

    document.getElementById("acAdd").addEventListener("click", function () { openForm(null); });
    document.getElementById("acImport").addEventListener("click", openImport);
    document.getElementById("acSem").addEventListener("change", function () {
      _sem = this.value;
      _page = 1;
      load();
    });
    document.getElementById("acSearch").addEventListener("input", function () {
      var v = this.value.trim();
      if (v === _q) return;
      _q = v;
      _page = 1;
      load();
    });
    document.getElementById("acPrev").addEventListener("click", function () {
      if (_page > 1) { _page -= 1; load(); }
    });
    document.getElementById("acNext").addEventListener("click", function () {
      _page += 1; load();
    });
    load();
  }

  function load() {
    var url = BASE + "?q=" + encodeURIComponent(_q) + (_sem ? "&semester=" + encodeURIComponent(_sem) : "") + "&page=" + _page + "&page_size=50";
    get(url).then(function (d) {
      var t = document.getElementById("acTotal");
      if (t) t.textContent = d.total;
      var rows = document.getElementById("acRows");
      var html = "";
      (d.admit_cards || []).forEach(function (r) {
        var n = (r.subjects || []).length;
        html += "<tr>" +
          "<td><b>" + esc(r.reg_no) + "</b></td>" +
          "<td>" + esc(r.name) + "</td>" +
          "<td>" + esc(r.semester) + "</td>" +
          "<td>" + esc(r.exam_type || "Regular") + "</td>" +
          "<td>" + esc(r.exam_session || "-") + "</td>" +
          "<td>" + esc(r.academic_year || "-") + "</td>" +
          "<td>" + esc(r.centre_name || "-") + "</td>" +
          "<td>" + esc(r.centre_code || "-") + "</td>" +
          "<td>" + esc(r.reporting_time || "-") + "</td>" +
          "<td>" + (n ? esc(n) + " subject" + (n === 1 ? "" : "s") : "-") + "</td>" +
          "<td>" + esc(r.issued_date || "-") + "</td>" +
          '<td style="text-align:right;white-space:nowrap;">' +
          '<button class="btn sm ghost" data-act="edit" data-id="' + esc(r.id) + '">Edit</button> ' +
          '<button class="btn sm danger" data-act="del" data-id="' + esc(r.id) + '">Withdraw</button>' +
          "</td></tr>";
      });
      if (!html) html = '<tr><td colspan="12" style="text-align:center;color:var(--muted);">No admit cards found.</td></tr>';
      rows.innerHTML = html;
      rows.querySelectorAll("[data-act]").forEach(function (b) {
        b.addEventListener("click", function () {
          var id = b.getAttribute("data-id");
          if (b.getAttribute("data-act") === "edit") openForm(id);
          else delCard(id);
        });
      });
      var pi = document.getElementById("acPageInfo");
      if (pi) {
        var pages = Math.max(1, Math.ceil(d.total / d.page_size));
        pi.textContent = "Page " + d.page + " of " + pages + " (" + d.total + " total)";
        document.getElementById("acNext").disabled = d.page >= pages;
        document.getElementById("acPrev").disabled = d.page <= 1;
      }
    }).catch(function (e) { toast(e.message, "error"); });
  }

  // ========== Create / Edit (single card) ==========
  function openForm(id) {
    var editing = null;
    function finalize() {
      var title = editing ? "Edit Admit Card" : "Issue Admit Card";
      var regField = editing ? "" :
        '<label style="margin-bottom:8px;"><b style="font-size:13px;">Registration Number <span style="color:#dc2626;">*</span></b>' +
        '<input id="ac_reg_no" type="text" class="st-input" style="width:100%;" placeholder="e.g. CUS-PR-A0001" required></label>';
      var subjects = editing ? (editing.subjects || []).join("\n") : "";
      var instructions = editing ? (editing.instructions || []).join("\n") : "";
      var semSelect = '<select id="ac_semester" class="st-input" style="width:100%;">' + _SEM_OPTIONS + "</select>";
      if (editing) {
        semSelect = semSelect.replace('<option value="' + editing.semester + '">', '<option value="' + editing.semester + '" selected>');
      }
      var body =
        '<label style="margin-bottom:8px;"><b style="font-size:13px;">Semester <span style="color:#dc2626;">*</span></b>' + semSelect + "</label>" +
        '<div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;">' +
        '<label style="margin-bottom:8px;"><b style="font-size:13px;">Exam Type</b>' +
        '<input id="ac_exam_type" type="text" class="st-input" style="width:100%;" value="' + esc((editing ? editing.exam_type : "Regular") || "") + '"></label>' +
        '<label style="margin-bottom:8px;"><b style="font-size:13px;">Exam Session</b>' +
        '<input id="ac_exam_session" type="text" class="st-input" style="width:100%;" placeholder="e.g. May/Jun 2025" value="' + esc((editing && editing.exam_session) || "") + '"></label>' +
        '<label style="margin-bottom:8px;"><b style="font-size:13px;">Academic Year</b>' +
        '<input id="ac_academic_year" type="text" class="st-input" style="width:100%;" placeholder="e.g. 2024-2025" value="' + esc((editing && editing.academic_year) || "") + '"></label>' +
        '<label style="margin-bottom:8px;"><b style="font-size:13px;">Issued Date</b>' +
        '<input id="ac_issued_date" type="text" class="st-input" style="width:100%;" placeholder="e.g. 01-May-2025" value="' + esc((editing && editing.issued_date) || "") + '"></label>' +
        "</div>" + regField +
        '<label style="margin-bottom:8px;"><b style="font-size:13px;">Centre Name <span style="color:#dc2626;">*</span></b>' +
        '<input id="ac_centre_name" type="text" class="st-input" style="width:100%;" value="' + esc((editing && editing.centre_name) || "") + '"></label>' +
        '<div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;">' +
        '<label style="margin-bottom:8px;"><b style="font-size:13px;">Centre Code</b>' +
        '<input id="ac_centre_code" type="text" class="st-input" style="width:100%;" value="' + esc((editing && editing.centre_code) || "") + '"></label>' +
        '<label style="margin-bottom:8px;"><b style="font-size:13px;">Reporting Time</b>' +
        '<input id="ac_reporting_time" type="text" class="st-input" style="width:100%;" placeholder="e.g. 09:00 AM" value="' + esc((editing && editing.reporting_time) || "") + '"></label>' +
        "</div>" +
        '<label style="margin-bottom:8px;"><b style="font-size:13px;">Centre Address</b>' +
        '<textarea id="ac_centre_address" class="st-input" style="width:100%;" rows="2">' + esc((editing && editing.centre_address) || "") + "</textarea></label>" +
        '<div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;">' +
        '<label style="margin-bottom:8px;"><b style="font-size:13px;">Subjects</b><span style="font-size:12px;color:var(--muted);"> one per line</span>' +
        '<textarea id="ac_subjects" class="st-input" style="width:100%;" rows="4">' + esc(subjects) + "</textarea></label>" +
        '<label style="margin-bottom:8px;"><b style="font-size:13px;">Instructions</b><span style="font-size:12px;color:var(--muted);"> one per line</span>' +
        '<textarea id="ac_instructions" class="st-input" style="width:100%;" rows="4">' + esc(instructions) + "</textarea></label>" +
        "</div>";
      var ov = document.createElement("div");
      ov.className = "modal-overlay";
      ov.style.display = "flex";
      ov.innerHTML =
        '<div class="modal-box">' +
        '<div class="modal-header"><h3>' + esc(title) + '</h3><button type="button" class="modal-close" data-mclose="1">&times;</button></div>' +
        '<div class="modal-body" style="padding:20px 24px;overflow-y:auto;max-height:70vh;">' + body +
        '<div id="acFormError" style="display:none;color:#dc2626;margin-top:10px;font-size:13px;"></div></div>' +
        '<div class="modal-footer"><button type="button" class="btn ghost" data-mclose="1">Cancel</button>' +
        '<button type="submit" class="btn green" id="acSave">' + esc(editing ? "Save Changes" : "Issue Card") + "</button></div>" +
        "</div>";
      document.body.appendChild(ov);
      ov.querySelectorAll("[data-mclose]").forEach(function (b) {
        b.addEventListener("click", function () { ov.remove(); });
      });
      ov.addEventListener("click", function (e) { if (e.target === ov) ov.remove(); });
      var form = document.createElement("form");
      form.style.display = "none";
      ov.appendChild(form);
      var saveBtn = ov.querySelector("#acSave");
      saveBtn.addEventListener("click", function () {
        var errEl = ov.querySelector("#acFormError");
        errEl.style.display = "none";
        var payload = {
          semester: parseInt(ov.querySelector("#ac_semester").value, 10),
          exam_type: ov.querySelector("#ac_exam_type").value.trim() || "Regular",
          exam_session: ov.querySelector("#ac_exam_session").value.trim(),
          academic_year: ov.querySelector("#ac_academic_year").value.trim(),
          issued_date: ov.querySelector("#ac_issued_date").value.trim(),
          centre_name: ov.querySelector("#ac_centre_name").value.trim(),
          centre_code: ov.querySelector("#ac_centre_code").value.trim(),
          reporting_time: ov.querySelector("#ac_reporting_time").value.trim(),
          centre_address: ov.querySelector("#ac_centre_address").value.trim(),
          subjects: ov.querySelector("#ac_subjects").value.split(/\r?\n/).map(function (x) { return x.trim(); }).filter(Boolean),
          instructions: ov.querySelector("#ac_instructions").value.split(/\r?\n/).map(function (x) { return x.trim(); }).filter(Boolean)
        };
        if (!editing) payload.reg_no = ov.querySelector("#ac_reg_no").value.trim();
        if (!payload.reg_no && !editing) { errEl.textContent = "Registration number is required."; errEl.style.display = "block"; return; }
        saveBtn.disabled = true;
        var p = editing ? patch(BASE + "/" + encodeURIComponent(editing.id), payload)
                        : post(BASE, payload);
        p.then(function (res) {
          ov.remove();
          toast(res.message || "Admit card " + (editing ? "updated" : "issued"), "success");
          load();
        }).catch(function (e) {
          errEl.textContent = e.message;
          errEl.style.display = "block";
          saveBtn.disabled = false;
        });
      });
    }
    if (id) {
      get(BASE + "?" + (new URLSearchParams({ q: _q, semester: _sem, page: _page, page_size: 100 })).toString()).then(function (d) {
        var found = (d.admit_cards || []).filter(function (c) { return String(c.id) === String(id); })[0];
        if (found) { editing = found; finalize(); }
        else { toast("That admit card no longer exists — refreshing.", "error"); _page = 1; load(); }
      }).catch(function (e) { toast(e.message, "error"); });
      return;
    }
    finalize();
  }

  function delCard(id) {
    if (!confirm("Withdraw this admit card? It will no longer be visible to the student.")) return;
    del(BASE + "/" + encodeURIComponent(id)).then(function () {
      toast("Admit card withdrawn", "success");
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
    ov.id = "acImportModal";
    ov.innerHTML =
      '<div class="modal-box" style="max-width:880px;">' +
      '<div class="modal-header"><h3>Import Admit Cards</h3><button type="button" class="modal-close" data-mclose="1">&times;</button></div>' +
      '<div id="acImportBody" style="padding:18px 24px;max-height:65vh;overflow-y:auto;"></div>' +
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
    var body = document.getElementById("acImportBody");
    body.innerHTML =
      '<p style="margin:0 0 10px;">Choose a <b>CSV</b> or <b>XLSX</b> file. Required columns: <b>Registration Number</b>,' +
      ' <b>Semester</b>, <b>Centre Name</b>. Optional: Exam Type, Exam Session, Academic Year, Centre Code, Centre Address,' +
      " Reporting Time, Subjects, Instructions, Issued Date.</p>" +
      '<p style="color:var(--muted);font-size:13px;margin:0 0 12px;">Subjects / Instructions are one entry per line (or a JSON array). ' +
      "Preview never writes anything. Confirming applies all rows in a single transaction — a duplicate or invalid row rejects the whole import.</p>" +
      '<input type="file" id="acFile" accept=".csv,.xlsx,text/csv,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" style="margin-bottom:14px;">' +
      '<div id="acFileErr" style="display:none;color:#dc2626;font-size:13px;margin-bottom:10px;"></div>' +
      '<div style="display:flex;gap:8px;"><button class="btn green" id="acAnalyze">Analyze file</button>' +
      '<button type="button" class="btn ghost" data-mclose="1">Cancel</button></div>';
    document.getElementById("acAnalyze").addEventListener("click", function () {
      var err = document.getElementById("acFileErr");
      var input = document.getElementById("acFile");
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
      document.getElementById("acAnalyze").disabled = true;
      upload(BASE + "/preview", fd).then(function (d) {
        _importFilename = d.filename || file.name;
        _importRawRows = d.raw_rows || [];
        _importRenderPreview(ov, d);
      }).catch(function (e) {
        snackIfAlive(err, e.message);
        document.getElementById("acAnalyze").disabled = false;
      });
    });
  }

  function snackIfAlive(el, msg) {
    if (el && el.isConnected) { el.textContent = msg; el.style.display = "block"; }
  }

  function _importRenderPreview(ov, d) {
    var body = document.getElementById("acImportBody");
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
        "<thead><tr><th>Row</th><th>Reg No</th><th>Sem</th><th>Exam</th><th>Year</th><th>Centre</th><th>Reporting</th></tr></thead><tbody>";
      d.rows.slice(0, 60).forEach(function (r) {
        validHtml += "<tr><td>" + esc(r.row) + "</td><td>" + esc(r.registration_number) + "</td><td>" + esc(r.semester) +
          "</td><td>" + esc(r.exam_type) + "</td><td>" + esc(r.academic_year || "-") + "</td><td>" + esc(r.centre_name) +
          "</td><td>" + esc(r.reporting_time || "-") + "</td></tr>";
      });
      if (d.valid_count > 60) validHtml += '<tr><td colspan="7" style="text-align:center;color:var(--muted);">…and ' + esc(d.valid_count - 60) + " more valid rows.</td></tr>";
      validHtml += "</tbody></table></div>";
    }
    var confirmBtn = "";
    if (d.error_count === 0 && d.valid_count > 0) {
      confirmBtn = '<div style="display:flex;gap:8px;margin-top:14px;">' +
        '<button class="btn green" id="acConfirm">Apply import (' + esc(d.valid_count) + " cards)</button>" +
        '<button type="button" class="btn ghost" data-mclose="1">Cancel</button></div>';
    } else {
      confirmBtn = '<div style="display:flex;gap:8px;margin-top:14px;">' +
        '<button type="button" class="btn ghost" data-mclose="1">Fix file and try again</button></div>';
    }
    body.innerHTML = stats + errHtml + validHtml + confirmBtn;
    var cb = document.getElementById("acConfirm");
    if (cb) cb.addEventListener("click", function () {
      cb.disabled = true;
      post(BASE + "/confirm", { filename: _importFilename, rows: _importRawRows }).then(function (res) {
        ov.remove();
        toast(res.message || "Admit cards imported", "success");
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

  window.CUS.admitCardsInit = render;
})();