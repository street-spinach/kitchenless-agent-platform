# Design sitemap

These are the approved designs that define what to build (CLAUDE.md, "Source of truth").
Each one is written with EGM (ENGINEERING_PROCESS.md §5). Where a design and
`PROJECT_BRIEF.md` disagree, the approved design wins.

| Design | Status | Date | Author | Components | Description |
|---|---|---|---|---|---|
| [architecture.md](architecture.md) | Approved (amended 2026-10-31, three times) | 2026-10-31 | madushankaf | `services/api`, `services/agents`, `services/mcp`, `services/mocks`, `services/safety`, `services/identity`, `services/common`, `services/seed`, `migrations/`, `infra/`, `evals/` | Services and boundaries, agent hosting and addressing, Postgres message delivery, data stores, weekly-order flow, failure handling, trace model, mock and real modes, 4 GB deployment, CI matrix, shared identifiers, provisional contracts, and bounded model choices for business agents |
| [agent-protocol.md](agent-protocol.md) | Approved | 2026-10-31 | madushankaf | `services/common/protocol`, `services/common` (outbox, consumer), `services/agents` (handlers, graphs, A2A endpoint), `services/mcp/household`, `services/api`, `migrations/`, `evals/` | Agent cards, message envelope, message catalog and schemas, negotiation state machine and A2A task mapping, sender check and protocol errors, summary lines and the live view, internal-only A2A JSON-RPC endpoint, shortfall flow after the courier is booked |
| agents.md | Planned | | | `services/agents` | Negotiation strategy, comparing partial offers, prompts for graphs and business choice points |
| identity-authz.md | Planned | | | `services/identity`, `authz/` | Keycloak delegation (`sub` and `act`), token sourcing for WhatsApp runs, OpenFGA model, budget conditions |
| pantry.md | Planned | | | `services/mcp/household` | Pantry events, confidence decay, photo recalibration, restock rules |
| adapters.md | Planned | | | `services/mcp`, `services/mocks` | MCP tool contracts, real adapters (Square, DoorDash, Kroger, Open Food Facts), mocks, control panel |
| experience.md | Planned | | | `services/api` (`/app`, `/me`), Expo app | Expo business app, consumer web pages, onboarding |
