# Design sitemap

These are the approved designs that define what to build (CLAUDE.md, "Source of truth").
Each one is written with EGM (ENGINEERING_PROCESS.md §5). Where a design and
`PROJECT_BRIEF.md` disagree, the approved design wins.

| Design | Status | Date | Author | Components | Description |
|---|---|---|---|---|---|
| [architecture.md](architecture.md) | Approved (amended 2026-10-31, three times; 2026-11-01, twice) | 2026-10-31 | madushankaf | `services/api`, `services/agents`, `services/mcp`, `services/mocks`, `services/safety`, `services/identity`, `services/common`, `services/seed`, `migrations/`, `infra/`, `evals/` | Services and boundaries, agent hosting and addressing, Postgres message delivery, data stores, weekly-order flow, failure handling, trace model, mock and real modes, 4 GB deployment, CI matrix, shared identifiers, provisional contracts, bounded model choices for business agents, and the MCP client contract (run context, bearer token, error results, retries) |
| [agent-protocol.md](agent-protocol.md) | Approved (amended 2026-11-01) | 2026-10-31 | madushankaf | `services/common/protocol`, `services/common` (outbox, consumer), `services/agents` (handlers, graphs, A2A endpoint), `services/mcp/household`, `services/api`, `migrations/`, `evals/` | Agent cards, message envelope, message catalog and schemas, negotiation state machine and A2A task mapping, sender check and protocol errors, summary lines and the live view, internal-only A2A JSON-RPC endpoint, shortfall flow after the courier is booked |
| agents.md | Planned | | | `services/agents` | Negotiation strategy, comparing partial offers, prompts for graphs and business choice points |
| [identity-authz.md](identity-authz.md) | Approved | 2026-11-01 | madushankaf | `services/identity`, `authz/`, `services/safety/scope.py`, `services/api/routes/me.py`, `services/api/routes/app.py`, `infra/keycloak/` | Keycloak 26.8 delegation (`sub` and `act`) with 30-day per-agent grants, token sourcing for runs with nobody present, magic-link consent, OpenFGA relations, spend bands and re-confirmation, revocation, household query scoping, service-to-service auth, audit |
| pantry.md | Planned | | | `services/mcp/household` | Pantry events, confidence decay, photo recalibration, restock rules |
| adapters.md | Planned | | | `services/mcp`, `services/mocks` | MCP tool contracts, real adapters (Square, DoorDash, Kroger, Open Food Facts), mocks, control panel |
| experience.md | Planned | | | `services/api` (`/app`, `/me`), Expo app | Expo business app, consumer web pages, onboarding |
