# Agentic food network

Hackathon platform. See [ENGINEERING_PROCESS.md](ENGINEERING_PROCESS.md) for how it is built.

## Architecture

![Kitchenless architecture: channels, agents, guardrails, MCP layer and data stores](Architecture.png)

More detail is under [Diagrams](#diagrams).

## Local quick-start

Needs Docker with the Compose plugin, `make`, and [uv](https://docs.astral.sh/uv/).

```bash
uv sync                                   # Python 3.12 deps (ruff, pytest, psycopg, pyyaml)
cp infra/.env.example infra/.env          # local defaults; MODE=mock, never commit infra/.env
make up                                   # build and start the stack, wait until healthy
make lint test-unit smoke                 # checks; smoke runs inside the Compose network
make eval-fast                            # fast eval suite against the running stack
make down                                 # stop (data volumes are kept)
```

Once the stack is up:

| What | Where |
|---|---|
| Health through Caddy | `curl -k https://localhost/health` |
| Hello agent run (writes trace rows) | `curl -k -X POST https://localhost/hello/run -d '{"text": "hi"}'` |
| Mock control panel | `https://localhost/panel` |
| Keycloak | `https://localhost/auth` (admin credentials from `infra/.env`) |
| Postgres, Redis, Neo4j, OpenFGA | `127.0.0.1` on 5432, 6379, 7474/7687, 8080/8081 |

Eval results are written to `evals/results/` (gitignored).

## Use the real models

By default, no model is called. The agents give fixed answers (`MODEL_MODE=stub`). All tests use this mode.

To use real models, you need two keys:

| Who uses it | Model | Where to get a key |
|---|---|---|
| Consumer and household agents | Muse Spark | Meta Model API (`api.meta.ai`) |
| Grocery, restaurant and delivery agents | Muse Spark Open | Baseten (`inference.baseten.co`) |

Never commit a key. Keys go only in `infra/.env`, which git ignores.

### On your laptop

1. Get the code and make your env file:

   ```bash
   git clone https://github.com/street-spinach/kitchenless-agent-platform.git
   cd kitchenless-agent-platform
   uv sync
   cp infra/.env.example infra/.env
   ```

2. Open `infra/.env` in an editor and set these lines:

   ```
   MODEL_MODE=real

   # Muse Spark, for consumer and household agents
   MODEL_BASE_URL=https://api.meta.ai/v1
   MODEL_NAME=<Muse Spark model id from Meta>
   MODEL_API_KEY=<your Meta key>

   # Muse Spark Open on Baseten, for business agents
   MODEL_BUSINESS_BASE_URL=https://inference.baseten.co/v1
   MODEL_BUSINESS_API_KEY=<your Baseten key>
   ```

   `MODEL_BUSINESS_NAME` is already `meta-models/muse-spark-open`, so you can leave it out.

   With `MODEL_MODE=real`, `agents` won't start if any of these values is empty. The one exception is a self-hosted business model that needs no key: set `MODEL_BUSINESS_SELF_HOSTED=true` and leave `MODEL_BUSINESS_API_KEY` empty. If you leave out `MODEL_BUSINESS_BASE_URL`, the Baseten host is used. If you set it to empty, business agents use `MODEL_BASE_URL` and `MODEL_API_KEY`.

   Optional: if Meta's API is down or blocked, the consumer and household agents can use Spark Open on Baseten as a backup:

   ```
   MODEL_FALLBACK_BASE_URL=https://inference.baseten.co/v1
   MODEL_FALLBACK_NAME=meta-models/muse-spark-open
   MODEL_FALLBACK_API_KEY=<your Baseten key>
   ```

3. Start the stack and check it:

   ```bash
   make up
   docker compose -f infra/docker-compose.yml --env-file infra/.env exec agents \
     python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8003/health').read().decode())"
   ```

   You should see `"model_mode": "real"` and `"model_business_name": "meta-models/muse-spark-open"`. `model_host` and `model_business_host` show the host each kind of agent calls, for example `"model_business_host": "inference.baseten.co"`.

4. To go back to fixed answers, set `MODEL_MODE=stub` in `infra/.env` and run `make up` again.

### On the VM

The VM keeps its own `infra/.env` in `/opt/app`. Nothing copies it from `.env.example`, so you add new lines by hand.

1. Log in:

   ```bash
   gcloud compute ssh madushankaf@spinach-vm --zone us-central1-a --project letsgomarket-8217a
   ```

2. Add the Baseten lines. The key is typed at a hidden prompt, so it is not saved in your shell history:

   ```bash
   cd /opt/app
   sudo cp infra/.env infra/.env.bak.$(date +%F-%H%M)
   sudo sed -i '/^MODEL_BUSINESS_\(NAME\|BASE_URL\|API_KEY\)=/d' infra/.env
   read -rsp 'Baseten key: ' KEY; echo
   printf 'MODEL_BUSINESS_BASE_URL=https://inference.baseten.co/v1\nMODEL_BUSINESS_API_KEY=%s\n' "$KEY" | sudo tee -a infra/.env >/dev/null
   unset KEY
   sudo grep -cE '^MODEL_BUSINESS_(BASE_URL|API_KEY)=' infra/.env    # prints 2
   ```

   Add the Meta lines (`MODEL_BASE_URL`, `MODEL_NAME`, `MODEL_API_KEY`) and `MODEL_MODE=real` the same way.

3. Apply the change: run the next deploy, or restart only the agents now:

   ```bash
   sudo -u deploy infra/eval-mode.sh restore
   ```

   Each deploy checks the build with fixed answers first. When `infra/.env` says `MODEL_MODE=real`, it then switches the agents back to the real models.

### Good to know

- Real mode costs money for every message. Turn it off when you are not testing.
- Business agents get 5 seconds and 300 tokens (a short answer) per step. If the model is slower, the agent uses a safe default worked out in code, and the order still goes ahead. To see how often that happens, look for `rule_default` in the `decision` trace events.
- Allergies, budgets and approvals are always checked in code. A real model cannot skip those checks.

## Diagrams

These are drawn from `docs/designs/architecture.md` (§2, §6, §8, §11), `agents.md`, `identity-authz.md`, `experience.md` and PR #393. They are written in Mermaid, which GitHub, VS Code and mermaid.live can show as pictures.

### 1. High-level view

Who uses the system, and what it talks to outside.

```mermaid
flowchart LR
    subgraph People
        M["Household members<br/>(WhatsApp)"]
        A["Household admin<br/>(WhatsApp + /me pages)"]
        O["Business owner<br/>(owner app, /owner)"]
    end

    subgraph K["Kitchenless platform"]
        P["AI agents<br/>plan, compare prices, agree a deal, order"]
        S["Safety rules in code<br/>allergies, diets, budget, approvals"]
    end

    subgraph Outside["Outside services"]
        WA["WhatsApp Cloud API<br/>(Meta)"]
        META["Meta Model API<br/>Muse Spark"]
        BT["Baseten<br/>Muse Spark Open"]
        KR["Kroger<br/>(catalog)"]
        SQ["Square<br/>(store, restaurant)"]
        DD["Courier<br/>(mock DoorDash)"]
    end

    M <--> WA
    A <--> WA
    WA <--> P
    A --> K
    O --> K
    P --- S
    P --> META
    P --> BT
    P --> KR
    P --> SQ
    P --> DD
```

### 2. Logical view

The parts of the system, and which part may talk to which.

```mermaid
flowchart TB
    subgraph Edge["Entry points (api)"]
        WH["WhatsApp webhook"]
        ME["/me pages"]
        OW["Owner app API"]
        PANEL["Control panel"]
        SEND["WhatsApp sender<br/>(24-hour window, templates)"]
    end

    subgraph Agents["agents (one process)"]
        direction TB
        C["Consumer agent<br/>one per person"]
        H["Household agent<br/>one per household"]
        G["Grocery agents"]
        R["Restaurant agents"]
        D["Delivery agents"]
        SW["Sweeper<br/>timers, expiry"]
        MC["model.py<br/>the only model caller"]
    end

    subgraph Tools["mcp (domain tool servers)"]
        GU["Guard<br/>token, OpenFGA, limits, trace"]
        AD["Adapters<br/>real or mock"]
    end

    subgraph Data["Data"]
        PG[("Postgres<br/>orders, rules, runs,<br/>messages, traces")]
        NEO[("Neo4j<br/>food graph")]
        FGA[("OpenFGA<br/>who may do what")]
        KC["Keycloak<br/>sign-in"]
    end

    WH --> C
    ME --> PG
    OW --> PG
    C <-->|"messages via Postgres outbox"| H
    H <-->|"quote, counter, accept"| G
    H <--> R
    G <--> D
    R <--> D
    H --> SEND
    C --> SEND

    C --> MC
    H --> MC
    G -->|"bounded choice"| MC
    R -->|"bounded choice"| MC
    D -->|"bounded choice"| MC

    C --> GU
    H --> GU
    G --> GU
    R --> GU
    D --> GU
    GU --> AD
    GU --> FGA
    GU --> PG
    AD --> NEO
    ME --> KC
    OW --> KC
```

Rules shown here:

- Agents reach business systems only through the guarded tools in `mcp`.
- Business agents get intent and constraints, never a person's raw profile.
- People agents use Muse Spark. Business agents use Muse Spark Open, and only to pick from a fixed set of allowed answers.

### 3. Physical view

What runs where, on the demo VM.

```mermaid
flowchart TB
    U["Phones and browsers"] -->|"HTTPS 443"| CADDY

    subgraph GCP["GCP project letsgomarket-8217a, us-central1-a"]
        subgraph VM["spinach-vm: e2-medium Spot, 4 GB RAM, 20 GB disk, Ubuntu 24.04"]
            CADDY["caddy<br/>:80 :443"]
            subgraph App["app containers (one image from GHCR)"]
                API["api :8000"]
                AGENTS["agents :8003"]
                MCP["mcp :8002"]
            end
            subgraph Infra["infrastructure containers"]
                PG[("postgres 16<br/>app, keycloak, openfga dbs")]
                NEO[("neo4j")]
                FGA["openfga"]
                KC["keycloak 26.8"]
            end
            ONE["one-shot: migrate, testrunner"]
        end
    end

    CADDY --> API
    CADDY -->|"/auth"| KC
    API --- PG
    AGENTS --- PG
    AGENTS -->|"MCP over HTTP"| MCP
    MCP --- PG
    MCP --- NEO
    MCP --- FGA
    API --- FGA
    KC --- PG

    GH["GitHub Actions<br/>build, then deploy over SSH"] -->|"push image"| GHCR["ghcr.io"]
    GH -->|"ssh deploy.sh"| VM
    GHCR -->|"docker pull"| VM

    AGENTS -->|"HTTPS"| META["api.meta.ai<br/>Muse Spark"]
    AGENTS -->|"HTTPS"| BT["inference.baseten.co<br/>Muse Spark Open"]
    API -->|"HTTPS"| WA["graph.facebook.com<br/>WhatsApp"]
    MCP -->|"HTTPS"| VEN["Kroger, Square"]
```

Memory caps for the long-running containers add up to about 3 GB of the VM's 4 GB.

### 4. Sequence: weekly grocery order (within budget)

```mermaid
sequenceDiagram
    autonumber
    actor P as Person
    participant WA as WhatsApp
    participant API as api
    participant C as Consumer agent
    participant H as Household agent
    participant G as Grocery agents
    participant D as Delivery agent

    P->>WA: "Do the weekly shop"
    WA->>API: webhook
    API->>API: check signature, drop repeats, phone to user, start run
    API->>C: user_message
    C->>H: start_weekly_order
    H->>H: create order (draft)
    H->>C: ask each member for constraints
    C-->>H: allergen codes, diet codes, dislikes only
    H->>H: build list (pantry, food graph checks)
    H->>G: quote_request to each store
    G-->>H: offers (price, stock)
    H->>G: up to 2 counter-offer rounds
    G-->>H: answers
    H->>H: pick cheapest valid offer, within budget
    H->>G: accept(order_id, items_hash)
    G->>D: ask couriers for quotes
    D-->>G: delivery quotes
    G->>G: place_order (placed)
    G->>D: delivery_request
    D->>D: book_delivery (courier_booked)
    D-->>H: delivery_booked
    Note over D,H: courier updates move the order to in_delivery, then delivered
    H->>API: weekly summary
    API->>WA: send
    WA->>P: "Your order is on its way"
```

### 5. Sequence: over budget, the admin approves

```mermaid
sequenceDiagram
    autonumber
    participant H as Household agent
    participant S as Safety (code)
    participant API as api sender
    actor AD as Admin
    participant SW as Sweeper
    participant G as Grocery agent

    H->>S: is the total over the remaining weekly budget?
    S-->>H: yes, approval needed
    H->>API: request_approval (state awaiting_approval)
    H->>SW: start 4-hour approval timer
    alt inside the 24-hour window
        API->>AD: text with Approve and Decline buttons
    else outside the window
        API->>AD: approved Meta utility template
    end
    alt admin taps Approve
        AD->>API: button tap
        API->>S: is this person the approver?
        S-->>API: yes
        API->>H: decide_approval(approved)
        H->>H: re-quote if the offer expired
        H->>G: accept
    else someone else taps
        API-->>AD: "Only the approver can do this"
    else no answer in 4 hours
        SW->>H: approval expired
        H->>H: order cancelled, run ends
    end
```

### 6. Sequence: one guarded tool call

Every tool that spends money, commits to an order or shares personal data goes through these steps.

```mermaid
sequenceDiagram
    autonumber
    participant AG as Agent
    participant GU as Guard (mcp)
    participant ID as Identity
    participant FGA as OpenFGA
    participant LIM as Limits (safety)
    participant AD as Adapter
    participant V as Vendor or mock
    participant TR as Trace writer

    AG->>GU: tool call + bearer token
    GU->>ID: check token (sub and act)
    ID-->>GU: ok
    GU->>FGA: may this agent do this, for this household?
    FGA-->>GU: allowed
    GU->>LIM: budget, allergies, approval rules (Postgres)
    LIM-->>GU: ok
    GU->>AD: call
    AD->>V: request (with idempotency key)
    V-->>AD: result
    GU->>TR: write trace event
    GU-->>AG: result
    Note over GU,AG: any check that fails refuses the call (fail closed)
```

### 7. Sequence: how a model call works

```mermaid
sequenceDiagram
    autonumber
    participant PA as People agent
    participant BA as Business agent
    participant M as model.py
    participant META as Meta: Muse Spark
    participant FB as Fallback: Spark Open
    participant BT as Baseten: Spark Open

    rect rgb(235, 242, 255)
    Note over PA,FB: People profile: consumer and household
    PA->>M: complete()
    M->>META: try 1 (20 s)
    META--xM: timeout or 5xx
    M->>META: try 2 (after 2 s)
    META--xM: fails again
    M->>FB: 1 try on the fallback
    FB-->>M: answer
    M-->>PA: result (or model_unavailable)
    end

    rect rgb(240, 250, 240)
    Note over BA,BT: Business profile: grocery, restaurant, delivery
    BA->>M: choose(allowed set, rule default)
    M->>BT: 1 try, 5 s, 300 tokens
    alt good answer inside the allowed set
        BT-->>M: pick + reason
        M-->>BA: model pick
    else slow, bad or out of bounds
        M-->>BA: rule default computed in code
    end
    end
```

### 8. Sequence: a deploy

```mermaid
sequenceDiagram
    autonumber
    participant GH as GitHub Actions
    participant GHCR as ghcr.io
    participant VM as spinach-vm (deploy.sh)

    GH->>GHCR: build and push image :sha
    GH->>VM: ssh, run deploy.sh
    VM->>VM: stop if DEPLOY_FREEZE exists
    VM->>GHCR: docker pull
    VM->>VM: up with MODEL_MODE=stub
    VM->>VM: seed, smoke, scenario
    alt checks pass
        VM->>VM: save .last_green
        opt infra/.env says MODEL_MODE=real
            VM->>VM: eval-mode.sh restore, agents back on real models
        end
    else checks fail
        VM->>VM: roll back to .last_green, smoke, exit 1
    end
```
