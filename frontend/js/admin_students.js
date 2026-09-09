(function () {
  "use strict";

  if (!window.CUS_API_BASE) throw new Error("CUS_API_BASE not defined");
  var API = window.CUS_API_BASE;
  var BASE = API + "/api/admin/students";

  function authHeaders() {
    var h = { "Content-Type": "application/json" };
    var t = localStorage.getItem("cus_admin_token");
    if (t) h.Authorization = "Bearer " + t;
    return h;
  }
  function log(m) { console.log("[CUS-Students] " + m); }

  function req(method, url, body) {
    var opts = { method: method, headers: authHeaders() };
    if (body !== undefined) opts.body = JSON.stringify(body);
    return fetch(url, opts).then(function (r) {
      if (r.status === 401) { log("Unauthorized, reloading"); window.location.reload(); throw new Error("Unauthorized"); }
      return r.json().then(function (d) { if (!r.ok) throw new Error((d && d.detail) || ("HTTP " + r.status)); return d; });
    });
  }
  function get(url) { return req("GET", url); }
  function post(url, body) { return req("POST", url, body); }
  function patch(url, body) { return req("PATCH", url, body); }
  function del(url) { return req("DELETE", url); }

  var esc = function (s) {
    return String(s === undefined || s === null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  };

  var ROOT_ID = "studentAdminRoot";
  var _page = 1;
  var _q = "";
  var _status = "";
  var _searchActive = false;
  var _searchPage = 1;
  var _searchMap = {};

  function root() { return document.getElementById(ROOT_ID); }

  function _field(label, name, value, opts) {
    opts = opts || {};
    var type = opts.type || "text";
    var extra = "";
    if (opts.required) extra += " required";
    if (opts.maxlength) extra += ' maxlength="' + opts.maxlength + '"';
    if (opts.disabled) extra += " disabled";
    if (opts.step) extra += ' step="' + opts.step + '"';
    if (opts.min) extra += ' min="' + opts.min + '"';
    if (opts.placeholder) extra += ' placeholder="' + esc(opts.placeholder) + '"';
    var input = '<input type="' + type + '" id="st_' + name + '" name="' + name + '"' +
      extra + ' value="' + esc(value === null || value === undefined ? "" : value) + '">';
    if (opts.choices) {
      var o = '<select id="st_' + name + '" name="' + name + '">';
      opts.choices.forEach(function (c) {
        o += '<option value="' + esc(c) + '"' + (String(value) === String(c) ? " selected" : "") + ">" + esc(c) + "</option>";
      });
      o += "</select>";
      input = o;
    }
    return '<label class="st-label">' + label + (opts.required ? ' <span style="color:#dc2626;">*</span>' : "") + input + "</label>";
  }

  function _readForm(names) {
    var out = {};
    names.forEach(function (n) {
      var el = document.getElementById("st_" + n);
      if (!el) return;
      var v = el.type === "checkbox" ? (el.checked ? "1" : "") : (el.value === null || el.value === undefined ? "" : String(el.value).trim());
      out[n] = v;
    });
    return out;
  }

  function _modalForm(title, bodyHtml, onSubmit, submitLabel) {
    var ov = document.createElement("div");
    ov.className = "modal-overlay";
    ov.style.display = "flex";
    ov.innerHTML =
      '<div class="modal-box">' +
      '<div class="modal-header"><h3>' + esc(title) + '</h3><button type="button" class="modal-close" data-mclose="1">&times;</button></div>' +
      '<form class="st-form">' +
      '<div class="modal-body" style="padding:20px 24px;overflow-y:auto;">' + bodyHtml +
      '<div id="stFormError" style="display:none;color:#dc2626;margin-top:10px;font-size:13px;"></div></div>' +
      '<div class="modal-footer"><button type="button" class="btn ghost" data-mclose="1">Cancel</button>' +
      '<button type="submit" class="btn green">' + esc(submitLabel || "Save") + '</button></div>' +
      "</form></div>";
    document.body.appendChild(ov);
    ov.querySelectorAll("[data-mclose]").forEach(function (b) {
      b.addEventListener("click", function () { ov.remove(); });
    });
    ov.addEventListener("click", function (e) {
      if (e.target === ov) ov.remove();
    });
    var errEl = ov.querySelector("#stFormError");
    ov.querySelector("form").addEventListener("submit", function (e) {
      e.preventDefault();
      if (errEl) errEl.style.display = "none";
      onSubmit(function (msg) {
        if (!errEl) return;
        errEl.textContent = msg;
        errEl.style.display = "block";
      });
    });
    return ov;
  }

  // ========== Render ==========
  function render() {
    if (!root()) return;
    root().innerHTML =
      '<div class="admin-card">' +
      "<h2>Student Services</h2>" +
      '<p class="sub">Manage student accounts, student results, admit cards and exam forms. A student&rsquo;s Date of Birth is their sign-in password ' +
      "(it is bcrypt-hashed server-side and never stored or returned in plain text).</p>" +
      '<div style="display:flex;gap:8px;margin-top:6px;">' +
      '<button class="btn sm" id="stNavStudents">Students</button>' +
      '<button class="btn sm ghost" id="stNavResults">Results</button>' +
      '<button class="btn sm ghost" id="stNavAdmitCards">Admit Cards</button>' +
      '<button class="btn sm ghost" id="stNavExamForms">Exam Forms</button>' +
      "</div>" +
      "</div>" +
      '<div id="stPaneStudents" class="admin-card">' +
      '<div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px;margin-bottom:12px;">' +
      '<div class="kpi kpi-sm" style="flex:1;min-width:160px;"><div class="box"><div class="n" id="stTotal">-</div>' +
      '<div class="l">Students</div></div></div>' +
      '<div style="display:flex;gap:8px;flex-wrap:wrap;">' +
      '<select id="stStatus" class="st-input"><option value="">All statuses</option>' +
      '<option value="active">Active</option><option value="inactive">Inactive</option></select>' +
      '<button class="btn green" id="stAdd">+ Add Student</button>' +
      "</div></div>" +
      '<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:14px;">' +
      '<span style="font-weight:600;font-size:13.5px;color:var(--navy);white-space:nowrap;">Search Student</span>' +
      '<input id="stSearch" type="search" class="st-input" placeholder="Name / Class Roll No / Registration No" style="flex:1;min-width:250px;">' +
      '<button class="btn sm green" id="stSearchBtn">Search</button>' +
      '<button class="btn sm ghost" id="stClearBtn">Clear</button>' +
      "</div>" +
      '<div id="stListBlock">' +
      '<div style="overflow-x:auto;"><table class="admin-table" style="width:100%;border-collapse:collapse;">' +
      "<thead><tr><th>Registration No</th><th>Name</th><th>Programme</th><th>Sem</th><th>College</th><th>Status</th><th></th></tr></thead>" +
      '<tbody id="stRows"><tr><td colspan="7" style="text-align:center;color:var(--muted);">Loading&hellip;</td></tr></tbody>' +
      "</table></div>" +
      '<div style="display:flex;justify-content:space-between;align-items:center;margin-top:14px;">' +
      '<span id="stPageInfo" style="color:var(--muted);font-size:13px;"></span>' +
      '<span style="display:flex;gap:8px;"><button class="btn sm ghost" id="stPrev">&#8592; Prev</button>' +
      '<button class="btn sm ghost" id="stNext">Next &#8594;</button></span>' +
      "</div></div>" +
      '<div id="stSearchBlock" style="display:none;">' +
      '<div style="overflow-x:auto;"><table class="admin-table" style="width:100%;border-collapse:collapse;">' +
      "<thead><tr><th>Name</th><th>Registration No</th><th>Class Roll</th><th>Exam Roll</th><th>Course</th><th>Semester</th><th></th></tr></thead>" +
      '<tbody id="stSearchRows"><tr><td colspan="7" style="text-align:center;color:var(--muted);">Searching&hellip;</td></tr></tbody>' +
      "</table></div>" +
      '<div style="display:flex;justify-content:space-between;align-items:center;margin-top:14px;">' +
      '<span id="stSearchInfo" style="color:var(--muted);font-size:13px;"></span>' +
      '<span style="display:flex;gap:8px;"><button class="btn sm ghost" id="stSePrev">&#8592; Prev</button>' +
      '<button class="btn sm ghost" id="stSeNext">Next &#8594;</button></span>' +
      "</div></div>" +
      "</div>" +
      '<div id="stPaneResults" style="display:none;"></div>' +
      '<div id="stPaneAdmitCards" style="display:none;"></div>' +
      '<div id="stPaneExamForms" style="display:none;"></div>';

    document.getElementById("stNavStudents").addEventListener("click", function () {
      document.getElementById("stNavStudents").className = "btn sm";
      document.getElementById("stNavResults").className = "btn sm ghost";
      document.getElementById("stNavAdmitCards").className = "btn sm ghost";
      document.getElementById("stNavExamForms").className = "btn sm ghost";
      document.getElementById("stPaneResults").style.display = "none";
      document.getElementById("stPaneAdmitCards").style.display = "none";
      document.getElementById("stPaneExamForms").style.display = "none";
      document.getElementById("stPaneStudents").style.display = "";
      refreshStudents();
    });
    document.getElementById("stNavResults").addEventListener("click", function () {
      document.getElementById("stNavResults").className = "btn sm";
      document.getElementById("stNavStudents").className = "btn sm ghost";
      document.getElementById("stNavAdmitCards").className = "btn sm ghost";
      document.getElementById("stNavExamForms").className = "btn sm ghost";
      document.getElementById("stPaneStudents").style.display = "none";
      document.getElementById("stPaneAdmitCards").style.display = "none";
      document.getElementById("stPaneExamForms").style.display = "none";
      document.getElementById("stPaneResults").style.display = "";
      if (window.CUS && window.CUS.studentResultsInit) window.CUS.studentResultsInit();
    });
    document.getElementById("stNavAdmitCards").addEventListener("click", function () {
      document.getElementById("stNavAdmitCards").className = "btn sm";
      document.getElementById("stNavStudents").className = "btn sm ghost";
      document.getElementById("stNavResults").className = "btn sm ghost";
      document.getElementById("stNavExamForms").className = "btn sm ghost";
      document.getElementById("stPaneStudents").style.display = "none";
      document.getElementById("stPaneResults").style.display = "none";
      document.getElementById("stPaneExamForms").style.display = "none";
      document.getElementById("stPaneAdmitCards").style.display = "";
      if (window.CUS && window.CUS.admitCardsInit) window.CUS.admitCardsInit();
    });
    document.getElementById("stNavExamForms").addEventListener("click", function () {
      document.getElementById("stNavExamForms").className = "btn sm";
      document.getElementById("stNavStudents").className = "btn sm ghost";
      document.getElementById("stNavResults").className = "btn sm ghost";
      document.getElementById("stNavAdmitCards").className = "btn sm ghost";
      document.getElementById("stPaneStudents").style.display = "none";
      document.getElementById("stPaneResults").style.display = "none";
      document.getElementById("stPaneAdmitCards").style.display = "none";
      document.getElementById("stPaneExamForms").style.display = "";
      if (window.CUS && window.CUS.examFormsInit) window.CUS.examFormsInit();
    });
    document.getElementById("stAdd").addEventListener("click", openCreate);
    document.getElementById("stStatus").addEventListener("change", function () {
      _status = this.value;
      if (_searchActive) { _searchPage = 1; loadSearch(); }
      else { _page = 1; load(); }
    });
    document.getElementById("stSearch").addEventListener("input", function () {
      var v = this.value.trim();
      if (v === _q) return;
      _q = v;
      _searchActive = false;
      _page = 1;
      load();
    });
    document.getElementById("stSearch").addEventListener("keydown", function (e) {
      if (e.key === "Enter") { e.preventDefault(); runSearch(); }
    });
    document.getElementById("stSearchBtn").addEventListener("click", runSearch);
    document.getElementById("stClearBtn").addEventListener("click", function () {
      document.getElementById("stSearch").value = "";
      _q = "";
      _searchActive = false;
      _page = 1;
      load();
    });
    document.getElementById("stSePrev").addEventListener("click", function () {
      if (_searchPage > 1) { _searchPage -= 1; loadSearch(); }
    });
    document.getElementById("stSeNext").addEventListener("click", function () {
      _searchPage += 1; loadSearch();
    });
    document.getElementById("stPrev").addEventListener("click", function () {
      if (_page > 1) { _page -= 1; load(); }
    });
    document.getElementById("stNext").addEventListener("click", function () {
      _page += 1; load();
    });
    load();
  }

  function load() {
    _setSearchMode(false);
    var url = BASE + "?q=" + encodeURIComponent(_q) + "&status=" + encodeURIComponent(_status) + "&page=" + _page + "&page_size=20";
    get(url).then(function (d) {
      var t = document.getElementById("stTotal");
      if (t) t.textContent = d.total;
      var rows = document.getElementById("stRows");
      var html = "";
      (d.students || []).forEach(function (s) {
        var active = s.is_active === true && s.status === "active";
        var badge = active ? '<span style="color:#15803d;font-weight:700;">Active</span>'
          : '<span style="color:#dc2626;font-weight:700;">Deactivated</span>';
        html += '<tr data-sid="' + esc(s.id) + '">' +
          '<td><b>' + esc(s.reg_no) + '</b></td>' +
          "<td>" + esc(s.name) + "</td>" +
          "<td>" + esc(s.programme) + "</td>" +
          "<td>" + esc(s.current_semester) + "</td>" +
          "<td>" + esc(s.college || "-") + "</td>" +
          "<td>" + badge + "</td>" +
          '<td style="text-align:right;white-space:nowrap;">' +
          '<button class="btn sm ghost" data-act="view" data-id="' + esc(s.id) + '">View</button> ' +
          '<button class="btn sm ghost" data-act="edit" data-id="' + esc(s.id) + '">Edit</button> ' +
          '<button class="btn sm ghost danger" data-act="delete" data-id="' + esc(s.id) + '">Delete</button> ' +
          '<button class="btn sm" data-act="reset" data-id="' + esc(s.id) + '">Reset DOB</button>' +
          "</td></tr>";
      });
      if (!html) html = '<tr><td colspan="7" style="text-align:center;color:var(--muted);">No students found.</td></tr>';
      rows.innerHTML = html;
      var pi = document.getElementById("stPageInfo");
      if (pi) {
        var pages = Math.max(1, Math.ceil(d.total / d.page_size));
        pi.textContent = "Page " + d.page + " of " + pages + " (" + d.total + " total)";
        document.getElementById("stNext").disabled = d.page >= pages;
        document.getElementById("stPrev").disabled = d.page <= 1;
      }
      rows.querySelectorAll("button[data-act]").forEach(function (b) {
        b.addEventListener("click", function () {
          var act = b.getAttribute("data-act");
          var id = b.getAttribute("data-id");
          if (act === "view") openDetail(id);
          else if (act === "edit") openEdit(id);
          else if (act === "reset") openReset(id);
          else if (act === "delete") openDelete(id);
        });
      });
    }).catch(function (e) { alert(e.message); });
  }

  // ========== Search Student (READ-ONLY, server-side) ==========
  function _setSearchMode(on) {
    var listBlock = document.getElementById("stListBlock");
    var searchBlock = document.getElementById("stSearchBlock");
    if (listBlock) listBlock.style.display = on ? "none" : "";
    if (searchBlock) searchBlock.style.display = on ? "" : "none";
  }

  function refreshStudents() {
    if (_searchActive) loadSearch(); else load();
  }

  function runSearch() {
    var v = (document.getElementById("stSearch").value || "").trim();
    if (!v) {
      document.getElementById("stSearch").value = "";
      _q = "";
      _searchActive = false;
      _page = 1;
      load();
      return;
    }
    _q = v;
    _searchActive = true;
    _searchPage = 1;
    loadSearch();
  }

  function loadSearch() {
    var term = (document.getElementById("stSearch").value || "").trim() || _q;
    var url = BASE + "/search?q=" + encodeURIComponent(term) +
      "&status=" + encodeURIComponent(_status) + "&page=" + _searchPage + "&page_size=20";
    get(url).then(function (d) { renderSearch(d); }).catch(function (e) { alert(e.message); });
  }

  function renderSearch(d) {
    _setSearchMode(true);
    var rows = document.getElementById("stSearchRows");
    var html = "";
    _searchMap = {};
    (d.students || []).forEach(function (r) {
      _searchMap[r.id] = r;
      var exam = r.exam_roll_no ? esc(r.exam_roll_no) : (r.exam_roll_conflict ? "&#9888; multiple" : "-");
      html += '<tr data-sid="' + esc(r.id) + '">' +
        "<td>" + esc(r.name) + "</td>" +
        '<td><b>' + esc(r.reg_no) + "</b></td>" +
        "<td>" + esc(r.roll_no || "-") + "</td>" +
        "<td>" + exam + "</td>" +
        "<td>" + esc(r.programme) + "</td>" +
        "<td>" + esc(r.current_semester) + "</td>" +
        '<td style="text-align:right;white-space:nowrap;">' +
        '<button class="btn sm ghost" data-act="sdetail" data-id="' + esc(r.id) + '">Details</button></td>' +
        "</tr>";
    });
    if (!html) {
      html = '<tr><td colspan="7" style="text-align:center;color:var(--muted);">' +
        "No student found matching your search.</td></tr>";
    }
    rows.innerHTML = html;
    var info = document.getElementById("stSearchInfo");
    if (info) {
      var pages = Math.max(1, Math.ceil(d.total / d.page_size));
      info.textContent = "Search results: " + d.total + " match(es) — page " + d.page + " of " + pages;
      document.getElementById("stSeNext").disabled = d.page >= pages;
      document.getElementById("stSePrev").disabled = d.page <= 1;
    }
    rows.querySelectorAll('button[data-act="sdetail"]').forEach(function (b) {
      b.addEventListener("click", function () {
        openSearchDetails(_searchMap[String(b.getAttribute("data-id"))]);
      });
    });
  }

  // ========== Search detail (safe 6-field card, no DOB anywhere) ==========
  function openSearchDetails(r) {
    if (!r) return;
    var exam = r.exam_roll_no
      ? esc(r.exam_roll_no)
      : (r.exam_roll_conflict
        ? '<em style="color:#b45309;">(inconsistent — see results)</em>'
        : "-");
    var body =
      _detailRow("Name", r.name) +
      _detailRow("Registration Number", r.reg_no) +
      _detailRow("Class/College Roll Number", r.roll_no) +
      '<div style="display:flex;justify-content:space-between;gap:12px;padding:7px 0;border-bottom:1px solid var(--line);">' +
      '<span style="color:var(--muted);">Examination Roll Number</span><strong>' + exam + "</strong></div>" +
      _detailRow("Course/Programme", r.programme) +
      _detailRow("Current Semester", r.current_semester);

    document.querySelectorAll(".modal-overlay").forEach(function (el) { el.remove(); });
    var ov = _modalForm("Student Details", body, function () {}, "Close");
    var footer = ov.querySelector(".modal-footer");
    if (footer) {
      footer.innerHTML = '<button type="button" class="btn green" data-mclose="1">Close</button>';
      var closeBtn = footer.querySelector('[data-mclose="1"]');
      if (closeBtn) closeBtn.addEventListener("click", function () { ov.remove(); });
    }
  }

  // ========== Create ==========
  function openCreate() {
    var body =
      '<div class="auth-form-row">' +
      _field("Registration Number", "reg_no", "", { required: true, maxlength: 50, placeholder: "e.g. CUS-2025-0001" }) +
      _field("Full Name", "name", "", { required: true, maxlength: 200 }) +
      "</div>" +
      '<div class="auth-form-row">' +
      _field("Date of Birth (password)", "dob", "", { required: true, type: "date" }) +
      _field("Programme", "programme", "", { required: true, maxlength: 50, placeholder: "e.g. bca" }) +
      "</div>" +
      '<div class="auth-form-row">' +
      _field("Current Semester", "current_semester", "1", { required: true, type: "number", min: 1, step: "1" }) +
      _field("Admission Year", "admission_year", "", { required: true, type: "number", min: 1990, step: "1" }) +
      "</div>" +
      '<div class="auth-form-row">' +
      _field("Roll No", "roll_no", "", { maxlength: 50 }) +
      _field("Gender", "gender", "Male", { choices: ["Male", "Female", "Other"] }) +
      "</div>" +
      '<div class="auth-form-row">' +
      _field("Father&rsquo;s Name", "father_name", "", { maxlength: 200 }) +
      _field("Mother&rsquo;s Name", "mother_name", "", { maxlength: 200 }) +
      "</div>" +
      '<div class="auth-form-row">' +
      _field("Email", "email", "", { maxlength: 255, type: "email" }) +
      _field("Phone", "phone", "", { maxlength: 20 }) +
      "</div>" +
      '<div class="auth-form-row">' +
      _field("College", "college", "", { maxlength: 200 }) +
      _field("Category", "category", "", { maxlength: 20 }) +
      "</div>" +
      '<div class="auth-form-row">' +
      _field("Academic Scheme", "academic_scheme", "", { maxlength: 20, placeholder: "cbcs | nep | nep2020" }) +
      _field("Batch", "batch", "", { maxlength: 20, placeholder: "e.g. 2025-2028" }) +
      "</div>" +
      _field("Address", "address", "", { maxlength: 2000 }) +
      '<div class="auth-form-row">' +
      _field("Status", "is_active", "Active", { choices: ["Active", "Inactive"] }) +
      "</div>" +
      '<p style="color:var(--muted);font-size:13px;margin-top:6px;">The date of birth becomes the student&rsquo;s sign-in password. ' +
      "It is bcrypt-hashed and will never be shown or returned after creation.</p>";

    _modalForm("Add Student", body, function (err) {
      var f = _readForm(["reg_no", "name", "dob", "programme", "current_semester", "admission_year", "roll_no",
        "gender", "father_name", "mother_name", "email", "phone", "college", "category", "academic_scheme",
        "batch", "address", "is_active"]);
      if (!f.reg_no || !f.name || !f.dob || !f.programme || !f.admission_year) { err("Required fields are missing."); return; }
      var payload = {
        reg_no: f.reg_no, name: f.name, dob: f.dob, programme: f.programme,
        current_semester: parseInt(f.current_semester || "1", 10) || 1,
        admission_year: parseInt(f.admission_year, 10),
        is_active: f.is_active === "Active",
      };
      ["roll_no", "father_name", "mother_name", "email", "phone", "college", "category", "academic_scheme",
        "batch", "address"].forEach(function (k) {
          if (f[k]) payload[k] = f[k];
        });
      if (f.gender) payload.gender = f.gender;
      post(BASE, payload).then(function () {
        document.querySelector(".modal-overlay") && Array.prototype.forEach.call(
          document.querySelectorAll(".modal-overlay"), function (el) { el.remove(); });
        toast("Student created", "success");
        _page = 1;
        load();
      }).catch(function (e) { err(e.message); });
    }, "Create Student");
  }

  // ========== Edit ==========
  function openEdit(id) {
    get(BASE + "/" + id).then(function (s) {
      document.querySelectorAll(".modal-overlay").forEach(function (el) { el.remove(); });
      var body =
        '<div class="auth-form-row">' +
        '<label class="st-label">Registration Number<input value="' + esc(s.reg_no) + '" disabled></label>' +
        _field("Full Name", "name", s.name, { required: true, maxlength: 200 }) +
        "</div>" +
        '<div class="auth-form-row">' +
        '<label class="st-label">Date of Birth<input value="' + esc(s.dob || "") + '" disabled></label>' +
        _field("Programme", "programme", s.programme, { maxlength: 50 }) +
        "</div>" +
        '<div class="auth-form-row">' +
        _field("Current Semester", "current_semester", s.current_semester, { type: "number", min: 1 }) +
        _field("Admission Year", "admission_year", s.admission_year, { type: "number", min: 1990 }) +
        "</div>" +
        '<div class="auth-form-row">' +
        _field("Roll No", "roll_no", s.roll_no, { maxlength: 50 }) +
        _field("Gender", "gender", s.gender || "Male", { choices: ["Male", "Female", "Other"] }) +
        "</div>" +
        '<div class="auth-form-row">' +
        _field("Father&rsquo;s Name", "father_name", s.father_name, { maxlength: 200 }) +
        _field("Mother&rsquo;s Name", "mother_name", s.mother_name, { maxlength: 200 }) +
        "</div>" +
        '<div class="auth-form-row">' +
        _field("Email", "email", s.email, { maxlength: 255, type: "email" }) +
        _field("Phone", "phone", s.phone, { maxlength: 20 }) +
        "</div>" +
        '<div class="auth-form-row">' +
        _field("College", "college", s.college, { maxlength: 200 }) +
        _field("Category", "category", s.category, { maxlength: 20 }) +
        "</div>" +
        '<div class="auth-form-row">' +
        _field("Academic Scheme", "academic_scheme", s.academic_scheme, { maxlength: 20 }) +
        _field("Batch", "batch", s.batch, { maxlength: 20 }) +
        "</div>" +
        _field("Address", "address", s.address, { maxlength: 2000 }) +
        '<p style="color:var(--muted);font-size:13px;">Registration number and Date of Birth are not editable here — ' +
        "Date of Birth is the student&rsquo;s password and changes only via <b>Reset DOB</b>.</p>";

      _modalForm("Edit Student", body, function (err) {
        var f = _readForm(["name", "programme", "current_semester", "admission_year", "roll_no", "gender",
          "father_name", "mother_name", "email", "phone", "college", "category", "academic_scheme", "batch", "address"]);
        if (!f.name) { err("Name is required."); return; }
        var payload = {};
        ["name", "roll_no", "father_name", "mother_name", "gender", "email", "phone", "college",
          "category", "academic_scheme", "batch", "address"].forEach(function (k) {
            if (f[k] !== "") payload[k] = f[k];
          });
        if (f.programme) payload.programme = f.programme;
        if (f.current_semester) payload.current_semester = parseInt(f.current_semester, 10);
        if (f.admission_year) payload.admission_year = parseInt(f.admission_year, 10);
        patch(BASE + "/" + id, payload).then(function () {
          document.querySelectorAll(".modal-overlay").forEach(function (el) { el.remove(); });
          toast("Student updated", "success");
          load();
        }).catch(function (e) { err(e.message); });
      }, "Save Changes");
    }).catch(function (e) { alert(e.message); });
  }

  // ========== Reset DOB ==========
  function openReset(id) {
    get(BASE + "/" + id).then(function (s) {
      document.querySelectorAll(".modal-overlay").forEach(function (el) { el.remove(); });
      var body =
        '<p style="color:var(--muted);font-size:14px;margin-bottom:12px;">Set a new Date of Birth for <b>' + esc(s.name) +
        "</b> (" + esc(s.reg_no) + "). This becomes their new sign-in password and revokes all existing sessions.</p>" +
        _field("New Date of Birth", "dob", "", { required: true, type: "date" });
      _modalForm("Reset DOB Password", body, function (err) {
        var f = _readForm(["dob"]);
        if (!f.dob) { err("A date of birth is required."); return; }
        post(BASE + "/" + id + "/reset-dob", { dob: f.dob }).then(function () {
          document.querySelectorAll(".modal-overlay").forEach(function (el) { el.remove(); });
          toast("DOB credential reset — all sessions revoked", "success");
          load();
        }).catch(function (e) { err(e.message); });
      }, "Reset DOB");
    }).catch(function (e) { alert(e.message); });
  }

  // ========== Delete (permanent) ==========
  function openDelete(id) {
    get(BASE + "/" + id).then(function (s) {
      document.querySelectorAll(".modal-overlay").forEach(function (el) { el.remove(); });
      var body =
        '<p style="color:#b91c1c;font-weight:700;font-size:14px;margin-bottom:10px;">&#9888; This action is permanent.</p>' +
        '<p style="color:var(--muted);font-size:14px;margin-bottom:12px;">Deleting <b>' + esc(s.name) +
        "</b> (" + esc(s.reg_no) + ") will remove the following data:</p>" +
        '<ul style="margin:0 0 14px;padding-left:20px;color:var(--muted);font-size:14px;line-height:1.7;">' +
        "<li>Student profile</li><li>Results</li><li>Admit Cards</li><li>Exam Forms</li><li>Sessions</li></ul>" +
        '<p style="color:#b91c1c;font-weight:700;font-size:14px;margin-bottom:10px;">This action cannot be undone.</p>' +
        _field("Type <b>DELETE</b> to continue", "delete_confirm", "", { required: true, maxlength: 20 });
      var ov = _modalForm("&#9888; Delete Student", body, function (err) {
        var typed = _readForm(["delete_confirm"]).delete_confirm;
        if (typed !== "DELETE") { err("Type DELETE to confirm the permanent delete."); return; }
        del(BASE + "/" + id).then(function () {
          document.querySelectorAll(".modal-overlay").forEach(function (el) { el.remove(); });
          toast("Student permanently deleted", "success");
          load();
        }).catch(function (e) { err(e.message); });
      }, "Delete");
      var submit = ov.querySelector('.modal-footer button[type="submit"]');
      if (submit) submit.className = "btn danger";
    }).catch(function (e) { alert(e.message); });
  }

  // ========== Detail ==========
  function _detailRow(label, value) {
    return '<div style="display:flex;justify-content:space-between;gap:12px;padding:7px 0;border-bottom:1px solid var(--line);">' +
      "<span style='color:var(--muted);'>" + esc(label) + "</span><strong>" + esc(value || "-") + "</strong></div>";
  }

  function openDetail(id) {
    get(BASE + "/" + id).then(function (s) {
      var active = s.is_active === true && s.status === "active";
      var body = "";
      body += _detailRow("Registration No", s.reg_no);
      body += _detailRow("Roll No", s.roll_no);
      body += _detailRow("Name", s.name);
      body += _detailRow("Date of Birth", "(write-only — use Reset DOB)");
      body += _detailRow("Father&rsquo;s Name", s.father_name);
      body += _detailRow("Mother&rsquo;s Name", s.mother_name);
      body += _detailRow("Gender", s.gender);
      body += _detailRow("Category", s.category);
      body += _detailRow("Email", s.email);
      body += _detailRow("Phone", s.phone);
      body += _detailRow("College", s.college);
      body += _detailRow("Programme", s.programme);
      body += _detailRow("Academic Scheme", s.academic_scheme);
      body += _detailRow("Semester", s.current_semester);
      body += _detailRow("Admission Year", s.admission_year);
      body += _detailRow("Batch", s.batch);
      body += _detailRow("Address", s.address);
      body += _detailRow("Status", active ? "Active" : "Deactivated");

      var footer =
        '<button type="button" class="btn ghost" data-act="edit">Edit</button> ' +
        '<button type="button" class="btn ghost" data-act="reset">Reset DOB</button> ' +
        '<button type="button" class="btn ghost danger" data-act="delete">Delete</button> ' +
        '<button type="button" class="btn green" data-mclose="1">Close</button>';

      document.querySelectorAll(".modal-overlay").forEach(function (el) { el.remove(); });
      var ov = _modalForm("Student Details", body, function () {}, "X");
      var form = ov.querySelector("form");
      if (form) {
        form.querySelector(".modal-footer").innerHTML = footer;
        form.querySelector(".modal-footer").querySelector("button[type=submit]").remove();
      }
      ov.querySelector('[data-act="edit"]').addEventListener("click", function () { ov.remove(); openEdit(id); });
      ov.querySelector('[data-act="reset"]').addEventListener("click", function () { ov.remove(); openReset(id); });
      ov.querySelector('[data-act="delete"]').addEventListener("click", function () { ov.remove(); openDelete(id); });
    }).catch(function (e) { alert(e.message); });
  }

  // ========== Toast (shared with admin panel) ==========
  function toast(msg, type) {
    if (window.CUS_TOAST) { window.CUS_TOAST(msg, type); return; }
    alert(msg);
  }

  // ========== Init ==========
  function init() {
    log("Initializing Student Services manager");
    render();
  }

  var initialized = false;
  function initOnce() {
    if (initialized) return;
    initialized = true;
    init();
  }

  window.CUS = window.CUS || {};
  window.CUS.studentAdminInit = initOnce;
})();

// Reusable CSS injected once, scoped to the students module.
(function () {
  "use strict";
  var css =
    ".st-label{display:flex;flex-direction:column;gap:6px;font-weight:600;font-size:13.5px;color:var(--navy);}" +
    ".st-label input,.st-label select,.st-input{padding:9px 12px;border:1px solid var(--border);border-radius:8px;" +
    "font-size:14px;font-family:inherit;width:100%;box-sizing:border-box;background:#fff;}" +
    ".st-label input:focus,.st-label select:focus,.st-input:focus{outline:none;border-color:var(--green);" +
    "box-shadow:0 0 0 3px rgba(26,158,99,.15);}" +
    ".st-label input:disabled,.st-label input[disabled]{background:#f3f5f4;color:var(--muted);cursor:not-allowed;}" +
    ".st-form .admin-table th,.st-form .admin-table td,#tabStudentServices .admin-table th," +
    "#tabStudentServices .admin-table td{padding:10px 12px;border-bottom:1px solid var(--line);text-align:left;}" +
    "#tabStudentServices .admin-table th{font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted);}" +
    "#tabStudentServices .modal-body{display:flex;flex-direction:column;gap:10px;}";
  var style = document.createElement("style");
  style.type = "text/css";
  style.appendChild(document.createTextNode(css));
  document.head.appendChild(style);
})();