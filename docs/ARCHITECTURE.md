# CUS AI Assistant — Architecture

## System Overview

```
┌─────────────────────────────────────────────────────────┐
│                    FRONTEND (Vanilla JS)                 │
│  HTML pages  ←──  chatbot.js  ←──  SSE stream           │
│                         │                               │
│                    navigates user                        │
│                    through chips, cards, forms            │
└────────────────────────┬────────────────────────────────┘
                         │ POST /api/chat/ask
                         │ Authorization: Bearer <JWT>
                         ▼
┌─────────────────────────────────────────────────────────┐
│              FASTAPI BACKEND (chat/routes.py)             │
│                                                          │
│  1. Validates input, rate-limits, authenticates          │
│  2. Delegates to Orchestrator Engine                     │
│  3. Converts engine events → SSE frames                  │
│  4. Migrates nav state on anon→real chat_id transition   │
└────────────────────────┬────────────────────────────────┘
                         │ orchestrator.engine.process()
                         ▼
┌─────────────────────────────────────────────────────────────┐
│              AI ORCHESTRATION ENGINE                         │
│                                                              │
│  ┌─────────────┐  ┌──────────────┐  ┌───────────────────┐   │
│  │ Intent      │  │ Conversation │  │ Service Router     │   │
│  │ Classifier  │  │ State        │  │                    │   │
│  │             │  │              │  │ Knowledge Engine  │   │
│  │ • nav       │  │ • breadcrumb │  │ Navigation Tree   │   │
│  │ • specific  │  │              │  └────────┬──────────┘   │
│  └──────┬──────┘  └──────────────┘           │               │
│         │ routes to                          │               │
│  ┌──────────────┐                            │               │
│  │  NAV intent  │ ──→ intent_router          │               │
│  │  KNOWLEDGE   │ ──→ run_chat (RAG)         │               │
│  └──────────────┘                            │               │
└──────────────────────────────────────────────────────────────┘
```

## Layer Architecture

### 1. Transport Layer (`chat/routes.py`)
- Thin SSE wrapper (120 lines)
- Handles input validation, JWT auth, rate limiting
- Converts engine event dicts → SSE wire format
- Manages anonymous→real chat_id migration

### 2. Orchestration Layer (`orchestrator/engine.py`)
- Single `process()` entry point for all messages
- Extended intent detection (navigation + knowledge)
- Routes to the correct handler based on intent + state
- Returns SSE-compatible dicts

### 3. State Layer (`orchestrator/state.py`)
- `ConversationState` per chat_id (in-memory dict)
- `Breadcrumb` trail for multi-step navigation
- TTL-based eviction (30 min inactivity)
- Async-safe with `asyncio.Lock`

### 4. Knowledge Layer (`chat/service.py` + `ingest/`)
- Unchanged from original architecture
- RAG pipeline: retrieve → format → generate → stream
- ChromaDB vector search, Ollama LLM
- Structured citation display

### 5. Navigation Layer (`chat/intent_router.py`)
- Unchanged core logic
- Keyword-based intent classification
- Complete navigation tree (programmes, fee, results info, etc.)
- Nav path tracking per chat_id

## SSE Event Protocol

| Event | Payload | Trigger |
|-------|---------|---------|
| `data: <token>` | Raw text token | LLM streaming |
| `event: options` | `{type, title, message, options[]}` | Navigation options |
| `event: detail` | `{type, title, fields[], actions[]}` | Information card |
| `event: done` | `{chat_id, cited_chunks[]}` | End of response |
| `event: error` | `{message}` | Error |

## Security Rules

1. **Credentials NEVER stored**: The assistant no longer collects student credentials in any form — student-portal queries (results, attendance, fee receipts, admit cards, transcripts, etc.) are answered from public knowledge only.
2. **JWT for API auth**: Existing Bearer token pattern unchanged
3. **HTTPS required**: All communication encrypted in production

## File Index

```
backend/app/
├── orchestrator/
│   ├── __init__.py          # Module docstring
│   ├── engine.py            # Central routing
│   ├── state.py             # Conversation state
│   ├── extractor.py         # Entity extraction (incl. semester/scheme)
│   ├── planner.py           # Plan routing (catalogue/rag/authority/grievance)
│   └── context.py           # Conversation context
├── chat/
│   ├── routes.py            # SSE transport
│   ├── service.py           # RAG pipeline
│   └── intent_router.py     # Navigation tree
└── ... (auth, ingest, admin, models, utils unchanged)

frontend/js/
├── chatbot.js               # Chat + SSE renderers (auth/service forms removed)
frontend/css/
├── chatbot.css              # Chat styling (auth/server-form styles removed)

docs/
├── ARCHITECTURE.md           # This file
├── API.md                    # API documentation
├── SEQUENCE_DIAGRAMS.md      # Interaction diagrams
└── EXTENSION_GUIDE.md        # How to extend the assistant
```
