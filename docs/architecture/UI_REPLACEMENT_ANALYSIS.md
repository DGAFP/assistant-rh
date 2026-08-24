# UI Replacement Options for assistant-rh RAG Chatbot

**Research Date:** April 2025
**Context:** French government HR chatbot (MATTE) replacing Streamlit UI
**Historical requirements:** OpenAI-compatible `/v1/chat/completions` endpoint, French language support, privacy constraints (no external telemetry), self-hostable hosting. The hosting target at the time was Scalingo; active deployment docs now target Scaleway.

---

> **Status note (2026-06)**: This is a historical research note from the earlier Scalingo hosting phase. Active assistant-rh deployment documentation now targets Scaleway; remaining Scalingo references below describe the original research assumptions, not current runtime guidance.

## Executive Summary

This analysis evaluates five categories of UI replacement options for the MATTE HR chatbot, focusing on OpenAI API compatibility, French localization, privacy compliance, and self-hosting feasibility on Scalingo (SecNumCloud).

**Key Findings:**

1. **suitenumerique/conversations** is the French government's official open-source chatbot project, purpose-built for public sector deployment with strong privacy guarantees and native French support. Early-stage but actively developed.

2. **Open WebUI** (124K+ stars) offers the most mature feature set with native RAG support, but uses a custom license that may restrict commercial/government use without review.

3. **LibreChat** (35.2K+ stars, MIT license) provides the best balance of maturity, full open-source licensing, and enterprise-grade authentication including Keycloak/OIDC support.

4. **Lobe Chat** (72K+ stars) has excellent French localization tooling but uses a non-commercial license.

5. **Lightweight native clients** (Askimo, Chatons) offer desktop-first experiences with full French UI but lack web deployment options.

**Primary Recommendation:** **suitenumerique/conversations** for strategic alignment with French government ecosystem, with **LibreChat** as a mature fallback option.

---

## Selection Criteria for assistant-rh Context

| Criterion | Requirement | Priority |
|-----------|-------------|----------|
| **OpenAI API Compatibility** | Must connect to `/v1/chat/completions` endpoint | Critical |
| **French Language Support** | Full UI localization, French HR domain terminology | Critical |
| **Privacy Compliance** | No external telemetry, GDPR compliant, SecNumCloud compatible | Critical |
| **Self-Hosting** | Deployable on Scalingo (PaaS) | Critical |
| **License** | Permissive for government use | High |
| **RAG Support** | Document retrieval for HR knowledge base | High |
| **Authentication** | OIDC/Keycloak integration for government SSO | High |
| **Maturity** | Production-ready or clear roadmap | Medium |
| **Community** | Active maintenance, support ecosystem | Medium |

---

## Option 1: suitenumerique/conversations (French Government Official)

### Overview

**Repository:** github.com/suitenumerique/conversations
**Stars:** 47 | **Forks:** 19 | **Contributors:** 20
**License:** MIT License
**Created:** 2024-06-26 | **Latest Release:** v0.0.15 (2025-03-31)
**Status:** Active development, early stage (warning: breaking changes may occur)

### Strategic Fit

This is the French government's official open-source AI chatbot project, designed for "La Suite numérique" ecosystem of tools for public services. It is explicitly built to be "simple, secure and privacy-friendly."

### Architecture & Tech Stack

| Component | Technology |
|-----------|------------|
| Frontend | Next.js SPA + Vercel AI SDK |
| Backend | Django Rest Framework + Pydantic AI |
| Database | PostgreSQL |
| Cache | Redis |
| Object Storage | S3-compatible (MinIO for dev) |
| Authentication | OIDC (Keycloak/ProConnect) |
| Deployment | Kubernetes (Helm charts), Docker Compose |
| Languages | Python 54.6%, TypeScript 31.1%, CSS 11.3% |

### OpenAI API Compatibility

✅ **Full compatibility confirmed**

- Provider `kind` field supports: `openai` or `mistral`
- Base URL configurable via `AI_BASE_URL` environment variable
- API key via `AI_API_KEY`
- Model name via `AI_MODEL`
- Supports any OpenAI-compatible endpoint including local Ollama
- Model settings: `max_tokens`, `temperature`, `top_p`, `timeout`, `parallel_tool_calls`, `seed`, `presence_penalty`, `frequency_penalty`

### Integration Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    suitenumerique/conversations             │
│  ┌─────────────┐   ┌──────────────────┐   ┌──────────────┐ │
│  │  Next.js    │──▶│  Django API      │──▶│  PostgreSQL  │ │
│  │  Frontend   │   │  (Pydantic AI)   │   │  (Redis)     │ │
│  └─────────────┘   └────────┬─────────┘   └──────────────┘ │
│                             │                               │
│                    AI_BASE_URL                              │
│                             │                               │
│                             ▼                               │
│              ┌──────────────────────────┐                   │
│              │  assistant-rh backend     │                   │
│              │  /v1/chat/completions    │                   │
│              └──────────────────────────┘                   │
└─────────────────────────────────────────────────────────────┘
```

### Self-Hosting Requirements

| Service | Dev Memory | Prod Memory |
|---------|------------|-------------|
| PostgreSQL | 1-2 GB | 2-8 GB |
| Keycloak/OIDC | ~1.3 GB | Variable |
| Redis | ≤256 MB | 256 MB - 2 GB |
| MinIO | 2 GB | 32 GB |
| Django API | 0.8-1.5 GB | 1-3 GB |
| Next.js frontend | 0.5-1 GB | N/A (static) |

**Minimum Hardware (prod ≤100 users):** 32 GB RAM, 8+ vCPU, 50+ GB SSD

### Deployment Methods

| Method | Status |
|--------|--------|
| Helm chart (Kubernetes) | ✅ Available |
| Docker Compose | 🔄 In Progress |
| YunoHost | 🔄 In Progress |
| Nix package | 📅 Coming Soon |

### Features

- ✅ Multi-model LLM support with JSON configuration
- ✅ Attachment support (images, PDFs, Office documents)
- ✅ RAG (Retrieval-Augmented Generation) for document search
- ✅ Web search tools (Brave, Tavily)
- ✅ Theming/customization via CSS injection
- ✅ Translation support via Crowdin
- ✅ Django admin interface

### Pros & Cons

| Pros | Cons |
|------|------|
| French government official project | Early stage (breaking changes warning) |
| Built for SecNumCloud compliance | Small community (47 stars) |
| Native ProConnect/OIDC support | Kubernetes required for production |
| MIT license (permissive) | Complex deployment stack |
| Active development (20 contributors) | Documentation in progress |
| Purpose-built for public sector | Scalingo compatibility untested |

### Scalingo Deployment Considerations

Scalingo is a PaaS platform; the Kubernetes Helm chart approach may not directly translate. Docker Compose deployment (in progress) would be more compatible. Key requirements to verify:

1. Multiple container support (Django API, frontend, Redis, PostgreSQL)
2. S3-compatible storage (Scalingo offers this)
3. OIDC provider availability (ProConnect integration)

---

## Option 2: Open WebUI

### Overview

**Repository:** github.com/open-webui/open-webui
**Stars:** 124K+ | **License:** Custom (NOT fully FOSS)
**Tech Stack:** Python (FastAPI), Preact/Svelte, SQLite

### OpenAI API Compatibility

✅ **Full compatibility**

- `/api/chat/completions` endpoint
- Customizable `OPENAI_API_BASE_URL`
- Works with any OpenAI-compatible backend

### French Localization

✅ **Supported**

- i18n support built-in
- fr-FR translation actively maintained
- PR #6450, #21602 for French improvements

### RAG Support

✅ **Native RAG**

- Document ingestion built-in
- Inline citations
- Knowledge base management

### Features

| Feature | Status |
|---------|--------|
| Multi-user support | ✅ Basic RBAC |
| Authentication | First user = super admin |
| RAG | ✅ Native |
| Document processing | ✅ PDF, images, Office |
| Web search | ✅ Integrations |
| Model switching | ✅ Multi-provider |

### Deployment

```bash
docker run -d -p 3000:8080 -v open-webui:/app/backend/data ghcr.io/open-webui/open-webui:main
```

- Docker: ✅ Full support
- Kubernetes: ✅ Helm, kustomize
- Hardware: Works on Raspberry Pi 5 (8GB)

### Privacy

✅ **No external telemetry** (self-hosted)
- User-controlled data
- Local storage by default

### Pros & Cons

| Pros | Cons |
|------|------|
| Largest community (124K+ stars) | Custom license (review before government use) |
| Most mature codebase | SQLite may limit scaling |
| Native RAG support | Basic auth (no OIDC integration documented) |
| Single container deployment | License uncertainty for public sector |
| Active French translation | |

### License Warning

⚠️ The custom license requires legal review before deployment in a French government context. MIT or Apache 2.0 licensed alternatives may be preferable.

---

## Option 3: LibreChat

### Overview

**Repository:** github.com/danny-avila/LibreChat
**Stars:** 35.2K+ | **License:** MIT (fully open-source)
**Tech Stack:** Node.js (Express/Fastify), React/Next.js, MongoDB, PostgreSQL (PGVector), Meilisearch

### OpenAI API Compatibility

✅ **Full compatibility**

- Native OpenAI support
- Custom endpoints for any OpenAI-compatible API

### French Localization

✅ **Supported**

- Multi-language support built-in
- PR #3240 merged (2024-07) for French translation updates

### RAG Support

✅ **LangChain + PostgreSQL (PGVector)**

### Authentication

✅ **Enterprise-grade**

- OAuth
- Azure AD
- AWS Cognito
- **Keycloak** ← Critical for ProConnect integration
- LDAP

### Deployment

| Method | Status |
|--------|--------|
| Docker | ✅ Full support |
| Docker Compose | ✅ Full support |
| npm | ✅ Available |
| Railway | ✅ One-click |
| Kubernetes | ✅ Helm |

**Minimum Requirements:** 1 GiB RAM, 1 vCPU (2GB recommended)

### Privacy

✅ **Privacy-focused, self-hosted**
- No external telemetry
- Full data control

### Pros & Cons

| Pros | Cons |
|------|------|
| MIT license (government-safe) | More complex than Open WebUI |
| Keycloak/OIDC support | MongoDB dependency |
| Active community (35K+ stars) | Higher deployment complexity |
| PGVector for RAG | |
| French translation merged | |
| Scalingo-friendly (Docker Compose) | |

---

## Option 4: Lobe Chat

### Overview

**Repository:** github.com/lobehub/lobe-chat
**Stars:** 72K+ | **License:** LobeHub Community License (non-commercial)

### OpenAI API Compatibility

✅ **Full compatibility**

- `OPENAI_API_KEY` environment variable
- `OPENAI_PROXY_URL` for custom endpoints

### French Localization

✅ **Excellent tooling**

- Lobe i18n automation tool
- "Xiao Zhi French Translation Assistant" agent
- Active translation community

### RAG Support

✅ **Knowledge Base**

- File upload
- Document processing

### Deployment

| Method | Status |
|--------|--------|
| Vercel | ✅ One-click |
| Docker | ✅ Full support |
| Zeabur | ✅ Available |

**Minimum:** 8GB+ RAM for local models via Ollama

### License Warning

⚠️ **LobeHub Community License** is free for personal and non-commercial use only. This likely excludes French government deployment without commercial licensing.

### Pros & Cons

| Pros | Cons |
|------|------|
| Best French localization tooling | Non-commercial license |
| Modern Next.js architecture | License excludes government use |
| Active community (72K+ stars) | |
| Beautiful UI | |

---

## Option 5: Lightweight Native/Electron Clients

### Desktop Client Comparison

| App | Tech Stack | OpenAI Compatible | French Support | Privacy | Self-Host |
|-----|------------|-------------------|----------------|---------|-----------|
| **Askimo** | Kotlin/Compose (native) | ✅ OpenAI, Anthropic, Mistral | ✅ Full French UI | ✅ 100% offline, encrypted keys | Desktop only |
| **Chatons** | Electron, TypeScript | ✅ Any OpenAI-compatible | ✅ French developer | ✅ No cloud sync, no telemetry | Desktop only |
| **PuPu** | JavaScript | ✅ OpenAI, Anthropic, Ollama | ✅ Mentioned | ✅ Local models | Desktop only |
| **TinyChat** | Python/tkinter | ✅ OpenAI, Anthropic, Mistral | ❓ Not specified | ✅ Local JSON keys | Desktop only |
| **Converse** | Electron | ✅ OpenAI, Anthropic, Mistral | ✅ Configurable | ✅ Local history | Desktop only |
| **Chatbox AI** | Cross-platform | ✅ Multi-provider | ✅ Multi-language | ✅ Local storage | Desktop only |

### Askimo Deep Dive

**Notable:** Native Kotlin/Compose (not Electron), resulting in better performance and lower resource usage.

**Features:**
- Full French UI
- 100% offline with local models
- Encrypted local API key storage
- Multi-provider support

### Privacy-Focused Self-Hosted Backends

| Tool | Telemetry | Data Retention | Offline | Deployment |
|------|-----------|----------------|---------|------------|
| **Jan** | None, disabled by default | Local only | 100% offline | Desktop app |
| **Ollama** | None, 100% local | User-controlled | Full offline | CLI + server |
| **LocalAI** | None | User-controlled | Full offline | Docker |
| **Open WebUI** | None (self-hosted) | User-controlled | Yes with local models | Docker |
| **GPT4All** | Opt-in, disabled by default | Local | Full offline | Desktop app |

### Pros & Cons

| Pros | Cons |
|------|------|
| Lowest resource footprint | Web deployment not available |
| Offline capability | Multiple user management limited |
| Privacy by design | Authentication integration limited |
| Native performance (Askimo) | Scalingo hosting incompatible |

### Applicability to assistant-rh

Desktop clients are **not suitable** as the primary UI replacement because:

1. Scalingo is a PaaS for web applications, not desktop distribution
2. MATTE requires web access for HR staff
3. Multi-user authentication (ProConnect/OIDC) requires server-side auth

However, these could be recommended as **optional desktop companions** for HR staff who prefer offline-capable tools.

---

## Comparison Matrix

### Feature Comparison

| Feature | conversations | Open WebUI | LibreChat | Lobe Chat | Askimo |
|---------|--------------|------------|-----------|-----------|--------|
| **OpenAI Compatible** | ✅ | ✅ | ✅ | ✅ | ✅ |
| **French UI** | ✅ Native | ✅ i18n | ✅ PR merged | ✅ Tooling | ✅ Full |
| **RAG Support** | ✅ | ✅ Native | ✅ PGVector | ✅ KB | ❌ |
| **OIDC/Keycloak** | ✅ ProConnect | ❌ | ✅ | 🟡 | ❌ |
| **MIT License** | ✅ | ❌ Custom | ✅ | ❌ Non-commercial | ✅ |
| **Docker Deploy** | 🔄 WIP | ✅ | ✅ | ✅ | ❌ Desktop |
| **Self-Hosted** | ✅ | ✅ | ✅ | ✅ | ✅ |
| **No Telemetry** | ✅ | ✅ | ✅ | ✅ | ✅ |
| **Scalingo Ready** | 🟡 Verify | ✅ | ✅ | ✅ | ❌ |
| **Maturity** | Early | High | High | High | Medium |
| **Community** | 47 ⭐ | 124K ⭐ | 35K ⭐ | 72K ⭐ | Small |

### License Comparison

| Project | License | Government Use |
|---------|---------|----------------|
| conversations | MIT | ✅ Permitted |
| Open WebUI | Custom | ⚠️ Review required |
| LibreChat | MIT | ✅ Permitted |
| Lobe Chat | LobeHub Community | ❌ Non-commercial only |
| Askimo | MIT | ✅ Permitted |

### Resource Requirements

| Project | Minimum RAM | Recommended | Database |
|---------|-------------|-------------|----------|
| conversations | 32 GB (full stack) | 32 GB+ | PostgreSQL |
| Open WebUI | 8 GB | 8 GB+ | SQLite |
| LibreChat | 2 GB | 4 GB+ | MongoDB/PostgreSQL |
| Lobe Chat | 8 GB (local models) | 8 GB+ | External |
| Askimo | 500 MB | 1 GB | Local |

---

## French Localization Analysis

### Native French Projects

1. **suitenumerique/conversations** - Built by French government, native French
2. **Chatons** - French developer, French-first

### Community French Translations

| Project | Status | Quality |
|---------|--------|---------|
| Open WebUI | ✅ Active (PR #6450, #21602) | High |
| LibreChat | ✅ Merged (PR #3240) | High |
| Lobe Chat | ✅ Automation tooling | High |
| Askimo | ✅ Full UI | Native |

### HR Domain Terminology

All projects will require customization for French HR terminology:
- "Agent" (civil servant)
- "Cadre" (executive)
- "Fonction publique" (civil service)
- "Arrêté" (administrative order)
- "Circulaire" (circular)

**Recommendation:** Create a French HR glossary for translation consistency across whichever UI is selected.

---

## Privacy & Self-Hosting Compliance

### Telemetry Analysis

| Project | External Telemetry | Data Exfiltration Risk |
|---------|-------------------|------------------------|
| conversations | ❌ None | ✅ None |
| Open WebUI | ❌ None (self-hosted) | ✅ None |
| LibreChat | ❌ None | ✅ None |
| Lobe Chat | ❌ None | ✅ None |
| Askimo | ❌ None | ✅ None |

### SecNumCloud Compliance Checklist

| Requirement | conversations | Open WebUI | LibreChat |
|-------------|--------------|------------|-----------|
| Data locality (EU) | ✅ Self-hosted | ✅ Self-hosted | ✅ Self-hosted |
| No third-party tracking | ✅ | ✅ | ✅ |
| Encryption at rest | ✅ PostgreSQL | ✅ SQLite | ✅ MongoDB/PG |
| Encryption in transit | ✅ TLS | ✅ TLS | ✅ TLS |
| Audit logging | ✅ Django admin | 🟡 Basic | 🟡 Requires config |
| GDPR compliance | ✅ Built-in | ✅ Self-hosted | ✅ Self-hosted |

### 2024-2025 Privacy Trends

Per gathered findings:
- 48% of enterprises have banned or restricted cloud AI (Cisco survey)
- Self-hosted AI adoption accelerating
- Local LLM quality approaching ChatGPT levels (DeepSeek R1, Mistral, Qwen)
- MCP (Model Context Protocol) emerging for tool integration

---

## Integration Architecture

### Recommended Architecture for assistant-rh

```
┌─────────────────────────────────────────────────────────────────────────┐
│                           Scalingo (SecNumCloud)                        │
│                                                                         │
│  ┌─────────────────────────────────────────────────────────────────┐   │
│  │                    Chat UI (conversations/LibreChat)             │   │
│  │  ┌─────────────┐   ┌──────────────┐   ┌──────────────────────┐   │   │
│  │  │  Frontend   │──▶│  Backend API │──▶│  PostgreSQL/Redis    │   │   │
│  │  │  (Next.js)  │   │  (Django/Node)│   │  (Scalingo add-ons) │   │   │
│  │  └─────────────┘   └──────┬───────┘   └──────────────────────┘   │   │
│  └──────────────────────────│───────────────────────────────────────┘   │
│                             │                                           │
│                             │ HTTP/REST                                  │
│                             ▼                                           │
│  ┌─────────────────────────────────────────────────────────────────┐   │
│  │                    assistant-rh Backend                          │   │
│  │  ┌─────────────────────────────────────────────────────────────┐ │   │
│  │  │  /v1/chat/completions (OpenAI-compatible)                    │ │   │
│  │  │  - RAG retrieval over HR documents                          │ │   │
│  │  │  - French HR domain context                                 │ │   │
│  │  │  - Mistral/OpenAI model routing                             │ │   │
│  │  └─────────────────────────────────────────────────────────────┘ │   │
│  └─────────────────────────────────────────────────────────────────┘   │
│                                                                         │
│  ┌─────────────────────────────────────────────────────────────────┐   │
│  │                    Authentication                                │   │
│  │  ProConnect (Keycloak) ←── OIDC ←── Chat UI                     │   │
│  └─────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────┘
```

### Environment Variables Required

```bash
# For suitenumerique/conversations
AI_BASE_URL=https://assistant-rh-backend.scalingo.io/v1
AI_API_KEY=${ASSISTANT_RH_API_KEY}
AI_MODEL=mistral-large

# For LibreChat
OPENAI_API_KEY=${ASSISTANT_RH_API_KEY}
OPENAI_API_BASE_URL=https://assistant-rh-backend.scalingo.io/v1
```

---

## Recommendation & Next Steps

### Primary Recommendation: suitenumerique/conversations

**Rationale:**

1. **Strategic Alignment** - Official French government project for public sector
2. **Privacy by Design** - Built for SecNumCloud compliance
3. **ProConnect Integration** - Native OIDC/Keycloak support for government SSO
4. **MIT License** - Fully permissive for government use
5. **French-First** - Native French development, no translation gaps
6. **Active Development** - 20 contributors, regular releases

**Risks:**

- Early stage project (breaking changes possible)
- Kubernetes deployment may require adaptation for Scalingo PaaS
- Smaller community for support

### Fallback Recommendation: LibreChat

**Rationale:**

1. **MIT License** - Government-safe
2. **Keycloak Support** - Compatible with ProConnect integration
3. **Mature Codebase** - 35K+ stars, production-ready
4. **Docker Compose** - Scalingo-compatible deployment
5. **French Translation** - Community-maintained, PR merged

### Implementation Roadmap

#### Phase 1: Validation (Week 1-2)

- [ ] Deploy suitenumerique/conversations locally via Docker Compose
- [ ] Test OpenAI-compatible endpoint integration with assistant-rh backend
- [ ] Verify French HR terminology displays correctly
- [ ] ProConnect/OIDC integration test

#### Phase 2: Scalingo Deployment (Week 3-4)

- [ ] Adapt Docker Compose for Scalingo deployment
- [ ] Configure PostgreSQL add-on
- [ ] Configure Redis add-on
- [ ] Set up S3-compatible storage (Scalingo Object Storage)
- [ ] Environment variable configuration

#### Phase 3: Integration (Week 5-6)

- [ ] Connect to assistant-rh backend `/v1/chat/completions`
- [ ] Configure HR knowledge base for RAG
- [ ] Customize French HR terminology
- [ ] User acceptance testing with HR staff

#### Phase 4: Production (Week 7-8)

- [ ] Security audit
- [ ] Load testing
- [ ] Monitoring setup
- [ ] Documentation
- [ ] Go-live

### Open Questions

1. **Scalingo Compatibility:** Has suitenumerique/conversations been tested on Scalingo PaaS? (Docker Compose deployment is in progress per findings)

2. **ProConnect Integration:** Does the OIDC integration work with ProConnect specifically, or does it require Keycloak as intermediary?

3. **Resource Requirements:** Can the full stack fit within Scalingo's resource limits? (32 GB minimum for production)

4. **Breaking Changes:** What is the project's commitment to backward compatibility given the early-stage warning?

5. **HR Customization:** What level of CSS/theming effort is required for MATTE branding?

### Contacts & Resources

- **suitenumerique/conversations:** github.com/suitenumerique/conversations
- **LibreChat:** github.com/danny-avila/LibreChat
- **La Suite numérique:** lasuite.numerique.gouv.fr
- **ProConnect:** proconnect.gouv.fr

---

## Appendix: 2024-2025 Industry Trends

Based on gathered findings:

1. **Self-Hosted AI Adoption** - 48% of enterprises have banned or restricted cloud AI tools (Cisco survey, 2024)

2. **Local LLM Quality** - Open-source models (DeepSeek R1, Mistral, Qwen) approaching ChatGPT performance

3. **Docker Standard** - Docker deployment now standard for self-hosted chat UIs

4. **MCP Emergence** - Model Context Protocol emerging as standard for tool integration

5. **Privacy-First Design** - Growing demand for telemetry-free, self-hosted solutions

---

*Report synthesized from parallel research batches. All claims attributable to gathered findings.*
