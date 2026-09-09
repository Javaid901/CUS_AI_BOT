(function () {
  "use strict";

  if (!window.CUS_API_BASE) throw new Error("CUS_API_BASE not defined");
  var API = window.CUS_API_BASE;
  var BASE = API + "/api/admin/results";

  function authHeaders() {
    var h = {};
    var t = localStorage.getItem("cus_admin_token");
    if (t) h.Authorization = "Bearer " + t;
    return h;
  }
  function log(m) { console.log("[CUS-Results] " + m); }

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
  function del(url) { return req("DELETE", url); }

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

  function root() { return document.getElementById("stPaneResults"); }
  function toast(msg, type) { if (window.CUS_TOAST) window.CUS_TOAST(msg, type || "success"); }

  // ========== Render ==========
  function render() {
    if (!root()) return;
    root().innerHTML =
      '<div class="admin-card">' +
      "<h2>Student Results</h2>" +
      '<p class="sub">Published results shown to students via Student Services. Import CSV or XLSX files — every row is ' +
      "validated (with an explicit preview) and applied in a single transaction. Duplicate rows block the import.</p>" +
      "</div>" +
      '<div class="admin-card">' +
      '<div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px;margin-bottom:16px;">' +
      '<div class="kpi kpi-sm" style="flex:1;min-width:160px;"><div class="box"><div class="n" id="srTotal">-</div>' +
      '<div class="l">Result entries</div></div></div>' +
      '<div style="display:flex;gap:8px;flex-wrap:wrap;">' +
      '<input id="srSearch" type="search" class="st-input" placeholder="Search reg no / name" style="min-width:220px;">' +
      '<select id="srSem" class="st-input"><option value="">All semesters</option>' +
      '<option value="1">Semester 1</option><option value="2">Semester 2</option><option value="3">Semester 3</option>' +
      '<option value="4">Semester 4</option><option value="5">Semester 5</option><option value="6">Semester 6</option>' +
      '<option value="7">Semester 7</option><option value="8">Semester 8</option></select>' +
      '<button class="btn green" id="srImport">&#8593; Import Results (CSV / XLSX)</button>' +
      "</div></div>" +
      '<div style="overflow-x:auto;"><table class="admin-table" style="width:100%;border-collapse:collapse;">' +
      "<thead><tr><th>Reg No</th><th>Name</th><th>Sem</th><th>Exam</th><th>Year</th><th>Exam Roll</th><th>Subject</th>" +
      "<th>Int</th><th>Ext</th><th>Total</th><th>Max</th><th>Grade</th><th>SGPA</th><th>CGPA</th><th>Status</th><th></th></tr></thead>" +
      '<tbody id="srRows"><tr><td colspan="16" style="text-align:center;color:var(--muted);">Loading&hellip;</td></tr></tbody>' +
      "</table></div>" +
      '<div style="display:flex;justify-content:space-between;align-items:center;margin-top:14px;">' +
      '<span id="srPageInfo" style="color:var(--muted);font-size:13px;"></span>' +
      '<span style="display:flex;gap:8px;"><button class="btn sm ghost" id="srPrev">&#8592; Prev</button>' +
      '<button class="btn sm ghost" id="srNext">Next &#8594;</button></span>' +
      "</div></div>";

    document.getElementById("srImport").addEventListener("click", openImport);
    document.getElementById("srSem").addEventListener("change", function () {
      _sem = this.value;
      _page = 1;
      load();
    });
    document.getElementById("srSearch").addEventListener("input", function () {
      var v = this.value.trim();
      if (v === _q) return;
      _q = v;
      _page = 1;
      load();
    });
    document.getElementById("srPrev").addEventListener("click", function () {
      if (_page > 1) { _page -= 1; load(); }
    });
    document.getElementById("srNext").addEventListener("click", function () {
      _page += 1; load();
    });
    load();
  }

  function load() {
    var url = BASE + "?q=" + encodeURIComponent(_q) + (_sem ? "&semester=" + encodeURIComponent(_sem) : "") + "&page=" + _page + "&page_size=50";
    get(url).then(function (d) {
      var t = document.getElementById("srTotal");
      if (t) t.textContent = d.total;
      var rows = document.getElementById("srRows");
      var html = "";
      var idMap = {};
      (d.results || []).forEach(function (r) {
        idMap[r.id] = r;
        var status = String(r.status || "").toUpperCase();
        var badge = status === "PASS" ? '<span style="color:#15803d;font-weight:700;">Pass</span>'
          : '<span style="color:#dc2626;font-weight:700;">Fail</span>';
        var subject = (r.subject_code ? esc(r.subject_code) + " " : "") + esc(r.subject_name);
        var marks = (r.total_marks === null || r.total_marks === undefined) ? "-" : esc(r.total_marks);
        var maxm = (r.max_marks === null || r.max_marks === undefined) ? "-" : esc(r.max_marks);
        html += "<tr>" +
          "<td><b>" + esc(r.reg_no) + "</b></td>" +
          "<td>" + esc(r.name) + "</td>" +
          "<td>" + esc(r.semester) + "</td>" +
          "<td>" + esc(r.exam_type || "Regular") + "</td>" +
          "<td>" + esc(r.academic_year || "-") + "</td>" +
          "<td>" + esc(r.exam_roll_no || "-") + "</td>" +
          "<td>" + subject + "</td>" +
          "<td>" + (r.internal_marks === null || r.internal_marks === undefined ? "-" : esc(r.internal_marks)) + "</td>" +
          "<td>" + (r.external_marks === null || r.external_marks === undefined ? "-" : esc(r.external_marks)) + "</td>" +
          "<td>" + marks + "</td>" +
          "<td>" + maxm + "</td>" +
          "<td>" + esc(r.grade || "-") + "</td>" +
          "<td>" + esc(r.sgpa || "-") + "</td>" +
          "<td>" + esc(r.cgpa || "-") + "</td>" +
          "<td>" + badge + "</td>" +
          '<td style="text-align:right;white-space:nowrap;"><button class="btn sm ghost danger" data-act="delete" data-id="' + esc(r.id) + '">Delete</button></td>' +
          "</tr>";
      });
      if (!html) html = '<tr><td colspan="16" style="text-align:center;color:var(--muted);">No results found.</td></tr>';
      rows.innerHTML = html;
      rows.querySelectorAll("button[data-act=delete]").forEach(function (b) {
        b.addEventListener("click", function () {
          var id = b.getAttribute("data-id");
          var row = idMap[id];
          if (row) openDeleteResult(id, row);
        });
      });
      var pi = document.getElementById("srPageInfo");
      if (pi) {
        var pages = Math.max(1, Math.ceil(d.total / d.page_size));
        pi.textContent = "Page " + d.page + " of " + pages + " (" + d.total + " total)";
        document.getElementById("srNext").disabled = d.page >= pages;
        document.getElementById("srPrev").disabled = d.page <= 1;
      }
    }).catch(function (e) { toast(e.message, "error"); });
  }

  // ========== Import ==========
  function openImport() {
    _importFilename = "";
    _importRawRows = [];
    var ov = document.createElement("div");
    ov.className = "modal-overlay";
    ov.style.display = "flex";
    ov.id = "srImportModal";
    ov.innerHTML =
      '<div class="modal-box" style="max-width:880px;">' +
      '<div class="modal-header"><h3>Import Results</h3><button type="button" class="modal-close" data-mclose="1">&times;</button></div>' +
      '<div id="srImportBody" style="padding:18px 24px;max-height:65vh;overflow-y:auto;"></div>' +
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
    var body = document.getElementById("srImportBody");
    body.innerHTML =
      '<p style="margin:0 0 10px;">Choose a <b>CSV</b> or <b>XLSX</b> file. Required columns: <b>Registration Number</b>,' +
      ' <b>Semester</b>, <b>Subject Name</b>. Optional: Examination Roll Number, Exam Type, Academic Year, Subject Code, Internal Marks, External Marks,' +
      " Total Marks, Max Marks, Grade, SGPA, CGPA, Status.</p>" +
      '<p style="color:var(--muted);font-size:13px;margin:0 0 12px;">Preview never writes anything. Confirming applies all rows in a ' +
      "single transaction — an import that contains a duplicate or invalid row is rejected as a whole.</p>" +
      '<input type="file" id="srFile" accept=".csv,.xlsx,text/csv,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" style="margin-bottom:14px;">' +
      '<div id="srFileErr" style="display:none;color:#dc2626;font-size:13px;margin-bottom:10px;"></div>' +
      '<div style="display:flex;gap:8px;"><button class="btn green" id="srAnalyze">Analyze file</button>' +
      '<button type="button" class="btn ghost" data-mclose="1">Cancel</button></div>';
    document.getElementById("srAnalyze").addEventListener("click", function () {
      var err = document.getElementById("srFileErr");
      var input = document.getElementById("srFile");
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
      document.getElementById("srAnalyze").disabled = true;
      upload(BASE + "/preview", fd).then(function (d) {
        _importFilename = d.filename || file.name;
        _importRawRows = d.raw_rows || [];
        _importRenderPreview(ov, d);
      }).catch(function (e) {
        snackIfAlive(err, e.message);
        document.getElementById("srAnalyze").disabled = false;
      });
    });
  }

  function snackIfAlive(el, msg) {
    if (el && el.isConnected) { el.textContent = msg; el.style.display = "block"; }
  }

  function _importRenderPreview(ov, d) {
    var body = document.getElementById("srImportBody");
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
        "<thead><tr><th>Row</th><th>Reg No</th><th>Sem</th><th>Exam Roll</th><th>Subject</th><th>Total</th><th>Max</th><th>Grade</th><th>Status</th></tr></thead><tbody>";
      d.rows.slice(0, 60).forEach(function (r) {
        validHtml += "<tr><td>" + esc(r.row) + "</td><td>" + esc(r.registration_number) + "</td><td>" + esc(r.semester) +
          "</td><td>" + esc(r.exam_roll_no || "-") +
          "</td><td>" + esc(r.subject_code ? r.subject_code + " " : "") + esc(r.subject_name) + "</td><td>" +
          (r.total_marks === null || r.total_marks === undefined ? "-" : esc(r.total_marks)) + "</td><td>" + esc(r.max_marks) +
          "</td><td>" + esc(r.grade || "-") + "</td><td>" + esc(r.status) + "</td></tr>";
      });
      if (d.valid_count > 60) validHtml += '<tr><td colspan="9" style="text-align:center;color:var(--muted);">…and ' + esc(d.valid_count - 60) + " more valid rows.</td></tr>";
      validHtml += "</tbody></table></div>";
    }
    var confirmBtn = "";
    if (d.error_count === 0 && d.valid_count > 0) {
      confirmBtn = '<div style="display:flex;gap:8px;margin-top:14px;">' +
        '<button class="btn green" id="srConfirm">Apply import (' + esc(d.valid_count) + " entries)</button>" +
        '<button type="button" class="btn ghost" data-mclose="1">Cancel</button></div>';
    } else {
      confirmBtn = '<div style="display:flex;gap:8px;margin-top:14px;">' +
        '<button type="button" class="btn ghost" data-mclose="1">Fix file and try again</button></div>';
    }
    body.innerHTML = stats + errHtml + validHtml + confirmBtn;
    var cb = document.getElementById("srConfirm");
    if (cb) cb.addEventListener("click", function () {
      cb.disabled = true;
      post(BASE + "/confirm", { filename: _importFilename, rows: _importRawRows }).then(function (res) {
        ov.remove();
        toast(res.message || "Results imported", "success");
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

  // ========== Delete result (single row, permanent) ==========
  function openDeleteResult(id, r) {
    var ov = document.createElement("div");
    ov.className = "modal-overlay";
    ov.style.display = "flex";
    ov.innerHTML =
      '<div class="modal-box" style="max-width:460px;">' +
      '<div class="modal-header"><h3>Delete Result</h3><button type="button" class="modal-close" data-mclose="1">&times;</button></div>' +
      '<div style="padding:18px 24px;">' +
      "<p style=\"margin:0 0 12px;font-size:14px;\">Delete Result?</p>" +
      '<div style="font-size:14px;line-height:1.8;margin-bottom:12px;">' +
      "<div>Student: <b>" + esc(r.name || "") + "</b> (" + esc(r.reg_no || "") + ")</div>" +
      "<div>Semester: <b>" + esc(r.semester || "") + "</b></div>" +
      "<div>Subject: <b>" + esc(r.subject_name || "") + "</b></div>" +
      "</div>" +
      '<p style="color:#b91c1c;font-weight:700;font-size:14px;margin:0;">This action is permanent.</p>' +
      "</div>" +
      '<div class="modal-footer">' +
      '<button type="button" class="btn ghost" data-mclose="1">Cancel</button> ' +
      '<button type="button" class="btn danger" id="srDelConfirm">Delete</button>' +
      "</div></div>";
    document.body.appendChild(ov);
    ov.querySelectorAll("[data-mclose]").forEach(function (b) {
      b.addEventListener("click", function () { ov.remove(); });
    });
    ov.addEventListener("click", function (e) { if (e.target === ov) ov.remove(); });
    var delBtn = document.getElementById("srDelConfirm");
    delBtn.addEventListener("click", function () {
      delBtn.disabled = true;
      del(BASE + "/" + id).then(function (res) {
        ov.remove();
        toast("Result deleted", "success");
        load();
      }).catch(function (e) {
        var err = document.createElement("div");
        err.style.color = "#dc2626"; err.style.fontSize = "13px"; err.style.marginTop = "10px";
        err.textContent = e.message;
        delBtn.parentNode.appendChild(err);
        delBtn.disabled = false;
      });
    });
  }

  window.CUS.studentResultsInit = render;
})();