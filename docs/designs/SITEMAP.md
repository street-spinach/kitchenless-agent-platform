# Design sitemap

These are the approved designs that define what to build (CLAUDE.md, "Source of truth").
Each one is written with EGM (ENGINEERING_PROCESS.md §5). Where a design and
`PROJECT_BRIEF.md` disagree, the approved design wins.

| Design | Status | Date | Author | Components | Description |
|---|---|---|---|---|---|
| [architecture.md](architecture.md) | Approved (amended 2026-10-31) | 2026-10-31 | madushankaf | `services/api`, `services/agents`, `services/mcp`, `services/mocks`, `services/safety`, `services/identity`, `services/common`, `services/seed`, `migrations/`, `infra/`, `evals/` | Services and boundaries, agent hosting and addressing, Postgres message delivery, data stores, weekly-order flow, failure handling, trace model, mock and real modes, 4 GB deployment, CI matrix, shared identifiers, provisional contracts, and bounded model choices for business agents |
| agent-protocol.md | Planned | | | `services/common` | Agent cards, message envelope and types, message persistence |
| identity-authz.md | Planned | | | `services/identity`, `authz/` | Keycloak delegation (`sub` and `act`), token sourcing for WhatsApp runs, OpenFGA model, budget conditions |
| pantry.md | Planned | | | `services/mcp/household` | Pantry events, confidence decay, photo recalibration, restock rules |
| adapters.md | Planned | | | `services/mcp`, `services/mocks` | MCP tool contracts, real adapters (Square, DoorDash, Kroger, Open Food Facts), mocks, control panel |
| experience.md | Planned | | | `services/api` (`/app`, `/me`), Expo app | Expo business app, consumer web pages, onboarding |
