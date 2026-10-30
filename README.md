# Agentic food network

Hackathon platform. See [ENGINEERING_PROCESS.md](ENGINEERING_PROCESS.md) for how it is built.

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
