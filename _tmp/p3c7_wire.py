# -*- coding: utf-8 -*-
"""Phase 3C-7 FINAL admin consolidation — deterministic wiring pass.

Applied against the CANONICAL git worktree at
C:\\Users\\LENOVO\\OneDrive\\Desktop\\CUS_AI_BOT  (git HEAD 7cbef9d).

Single, self-contained edit pass:
  a) admin.html
       1. REMOVE the duplicate "Universal Notices" (data-tab="notices")
          nav button.
       2. REPLACE the legacy <div id="tabSyncDocuments"> panel with a new
          <div id="tabUniversityDocuments"> panel whose mount root is
          `universityDocumentsRoot` (consumed by
          frontend/js/admin_university_documents.js).
       3. REMOVE the legacy <div id="tabNotices"> panel.
       4. ADD the script include for admin_university_documents.js.
  b) admin.js
       1. Rewire tab dispatch: data-tab="syncDocuments" ->
          data-tab="universityDocuments" calling
          window.CUS.universityDocumentsInit().
       2. REMOVE the data-tab="notices" dispatch branch (the Universal
          Notices admin view is superseded by University Documents).

No second table/system/router is created.  Legacy JS payloads
(admin_sync_documents.js / admin_notices.js) remain on disk and stay
included for internal compatibility, but are no longer reachable from
the admin nav.  Backend + model + service + student-facing behaviour are
untouched.  Every replacement is literal and count-guarded (exactly 1).
"""

import io
import os
import sys

ROOT = r"C:\Users\LENOVO\OneDrive\Desktop\CUS_AI_BOT"
ADM_HTML = os.path.join(ROOT, "frontend", "pages", "admin.html")
ADM_JS = os.path.join(ROOT, "frontend", "js", "admin.js")


def read(p):
    with io.open(p, "r", encoding="utf-8", newline="") as f:
        return f.read()


def write(p, s):
    with io.open(p, "w", encoding="utf-8", newline="") as f:
        f.write(s)


def rep_once(tag, text, old, new):
    n = text.count(old)
    if n != 1:
        raise SystemExit("[FAIL] %s: expected 1 occurrence, found %d" % (tag, n))
    return text.replace(old, new)


def drop_line(text, needle):
    # removes the entire line containing needle (exactly one such line)
    lines = text.split("\n")
    hit = [i for i, ln in enumerate(lines) if needle in ln]
    if len(hit) != 1:
        raise SystemExit("[FAIL] drop_line(%r): expected 1 matching line, found %d"
                         % (needle, len(hit)))
    i = hit[0]
    # keep surrounding blank-line structure tidy: drop the full line
    lines.pop(i)
    return "\n".join(lines)


# ----------------------------------------------------------------------
# admin.html
# ----------------------------------------------------------------------
a = read(ADM_HTML)

# 1) remove the Universal Notices nav button line
a = drop_line(a, 'data-tab="notices">')

# 2) replace legacy tabSyncDocuments panel with the consolidated panel
OLD_SYNC_PANEL = (
    '        <!-- === Sync Documents Tab === -->\n'
    '        <div id="tabSyncDocuments" class="tab-content" style="display:none;">\n'
    '          <div class="admin-card" id="syncDocumentsRoot"><p class="sub">Loading document review queue...</p></div>\n'
    '        </div>\n'
)
NEW_UD_PANEL = (
    '        <!-- === University Documents Tab (Consolidated: crawler + manual upload) === -->\n'
    '        <div id="tabUniversityDocuments" class="tab-content" style="display:none;">\n'
    '          <div class="admin-card" id="universityDocumentsRoot"></div>\n'
    '        </div>\n'
)
a = rep_once("sync-panel->ud-panel", a, OLD_SYNC_PANEL, NEW_UD_PANEL)

# 3) remove legacy tabNotices panel
OLD_NOTICES_PANEL = (
    '        <!-- === University Notices Tab === -->\n'
    '        <div id="tabNotices" class="tab-content" style="display:none;">\n'
    '          <div id="noticesAdminRoot" style="min-height:200px;"></div>\n'
    '        </div>\n'
)
a = rep_once("notices-panel-removal", a, OLD_NOTICES_PANEL, "")

# 4) add script include for the consolidated module (right after the
#    legacy sync documents include, before the closing tag)
INC_LEGACY = '  <script src="../js/admin_sync_documents.js?v=2"></script>\n'
INC_NEW = INC_LEGACY + '  <script src="../js/admin_university_documents.js?v=1"></script>\n'
a = rep_once("script-include-add", a, INC_LEGACY, INC_NEW)

write(ADM_HTML, a)

# ----------------------------------------------------------------------
# admin.js
# ----------------------------------------------------------------------
j = read(ADM_JS)

# 1) rewire dispatch: syncDocuments -> universityDocuments
OLD_SYNC_DISPATCH = (
    '      if (btn.dataset.tab === "syncDocuments") {\n'
    '        if (window.CUS && window.CUS.syncDocumentsInit) window.CUS.syncDocumentsInit();\n'
    '      }\n'
)
NEW_UD_DISPATCH = (
    '      if (btn.dataset.tab === "universityDocuments") {\n'
    '        if (window.CUS && window.CUS.universityDocumentsInit) window.CUS.universityDocumentsInit();\n'
    '      }\n'
)
j = rep_once("dispatch-sync->ud", j, OLD_SYNC_DISPATCH, NEW_UD_DISPATCH)

# 2) remove the notices dispatch branch (superseded by University Documents)
OLD_NOTICES_DISPATCH = (
    '      if (btn.dataset.tab === "notices") {\n'
    '        if (window.CUS && window.CUS.noticesAdminInit) window.CUS.noticesAdminInit();\n'
    '      }\n'
)

# delete the block cleanly (including trailing blank line if adjacent)
def del_block(text, block):
    n = text.count(block)
    if n != 1:
        raise SystemExit("[FAIL] del_block: expected 1 occurrence, found %d" % n)
    return text.replace(block, "")

j = del_block(j, OLD_NOTICES_DISPATCH)
# tidy a trailing leftover blank line that immediately preceded it
import re as _re
j = _re.sub(r"\n\n\n( +}\n +});\n)", r"\n\n\1", j, count=1)

write(ADM_JS, j)

# ----------------------------------------------------------------------
print("OK — admin.html + admin.js wired.")
print("  admin.html: notices button removed; tabSyncDocuments -> tabUniversityDocuments;")
print("              tabNotices removed; admin_university_documents.js included.")
print("  admin.js:   dispatch syncDocuments -> universityDocuments; notices branch removed.")
