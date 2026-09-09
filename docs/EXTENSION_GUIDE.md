# Extension Guide — Adding University Information

## Overview

The CUS AI Assistant answers university-related questions from structured data
and the website knowledge base. The legacy "student service connector"
framework (`services/`, student auth/session flow) has been **removed** —
the assistant never collects credentials or fetches personal portal data.
Answers are grounded in public, curated information only.

You extend the assistant by enriching its **data sources**, not by adding
execution connectors:

## 1. Academic Catalogue (programmes, fee, eligibility, subjects)

Edit `backend/app/catalogue/seed.py` or manage via the admin upload flow
(`/admin`). This is the authoritative source for `catalogue` plans
(fee structure, eligibility, semester subjects, credits, NEP scheme, etc.).

## 2. Website Knowledge (RAG)

Upload documents via the admin uploader (`POST /api/admin/upload`). The
ingest pipeline (`backend/app/ingest/`) indexes them into the vector store;
`chat/service.py` retrieves and answers with citations. News/notices are
kept in sync by the website sync scheduler.

## 3. Authorities & Grievance routing

Manage authority records (offices, contact details, keywords, grievance
actions) in the admin (`/admin` → authorities) or the seed data used by
`app/authority/matcher.py`. The planner routes office queries to these
records and the grievance flow uses them for intake.

## 4. Navigation tree

`backend/app/chat/intent_router.py` defines the top-level topics and
sub-options (programmes, fee, colleges, examinations, contact…). Add new
categories to `_TOPICS`, `_BROAD_KEYWORDS`, and `WELCOME_OPTIONS` to expose
new navigation chips. Do NOT add entry points that imply personal portal data.

## 5. Intent knowledge base

`backend/app/orchestrator/intent_kb.py` holds canonical intents + paraphrases
used by the semantic intent classifier. Add or adjust paraphrases there; the
classifier re-embeds automatically when the paraphrase fingerprint changes.

## Testing

Run the acceptance batteries in `backend/tests/`:

```
python tests/test_smart_orchestrator.py   # planner routing incl. negative SS checks
python tests/test_intelligence.py         # extractor + planner + engine e2e
python tests/test_intent_matrix.py        # greeting / grievance / authority / fallback
```

Rule of thumb: any query mentioning a personal student record (results,
attendance, fee receipt, admit card, transcript…) must never reach an
`auth_form`/credential/session flow — it should fall through to public
navigation/knowledge.