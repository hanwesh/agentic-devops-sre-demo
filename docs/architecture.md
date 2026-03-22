# Architecture

## System Overview

```
┌─────────────┐    push/merge     ┌──────────────────┐    deploy    ┌──────────────────────────────┐
│  Developer   │ ───────────────► │  GitHub Actions   │ ──────────► │  Azure App Service (Linux)   │
│  (Human)     │                  │  CI/CD Pipeline   │             │  Python 3.12 + FastAPI       │
└──────┬───────┘                  └──────────────────┘             │  + Azure PostgreSQL (Std)    │
       │                                   ▲                       │  + Application Insights      │
       │ review PR                         │ merge triggers        └────────────┬─────────────────┘
       │                                   │ redeploy                           │
┌──────▼───────┐    creates PR    ┌────────┴─────────┐  assigns   ┌────────────▼─────────────────┐
│  Pull Request │ ◄────────────── │ Copilot Coding   │ ◄───────── │  Azure SRE Agent             │
│  (for review) │                 │ Agent (Opus 4.6) │  via GH    │  → detects incidents         │
└──────────────┘                  └──────────────────┘  Actions   │  → collects logs/screenshots │
                                                                   │  → creates GitHub Issue      │
                                                                   └──────────────────────────────┘
```

## Components

### Application Layer
- **FastAPI** web framework serving a REST API for task management
- **SQLAlchemy 2.0** (async) ORM with PostgreSQL via `asyncpg`
- **Alembic** for database schema migrations
- **Pydantic** for request validation and response serialization

### Azure Infrastructure
| Resource | SKU | Purpose |
|----------|-----|---------|
| App Service Plan | Standard S1 (Linux) | Compute for the web app |
| App Service | Python 3.12 | Web app with staging + production slots |
| PostgreSQL Flexible Server | Standard_D2s_v3 | Persistent data store |
| Application Insights | Standard | APM, distributed tracing, logging |
| Log Analytics Workspace | Standard | Centralized log aggregation |

### CI/CD Pipelines
1. **CI** (`ci.yml`) — Runs on every push: lint (ruff), type check (mypy), test (pytest), security (pip-audit)
2. **CD** (`cd.yml`) — Runs on push to `main`: build → deploy to staging → smoke test → swap to production
3. **SRE Triage** (`sre-issue-triage.yml`) — Auto-triages SRE incidents: parse severity → label → assign Copilot

### Agentic Loop
1. Azure SRE Agent monitors Application Insights for anomalies
2. On incident detection, SRE Agent creates a GitHub Issue with:
   - Stack trace, error summary, performance metrics
   - AI-generated root cause analysis
   - Suggested code fix
3. GitHub Actions triage workflow adds priority labels and assigns Copilot Coding Agent
4. Copilot analyzes the issue, creates a branch, implements the fix + tests, opens a PR
5. Human reviews and merges → CI/CD deploys → SRE Agent confirms resolution

## Data Flow

```
User Request → App Service → FastAPI → SQLAlchemy → PostgreSQL
                  │
                  ├── Logs → Application Insights → Log Analytics
                  │                                      │
                  │                              Azure SRE Agent
                  │                                      │
                  │                              GitHub Issue
                  │                                      │
                  │                              Triage Workflow
                  │                                      │
                  │                              Copilot Agent
                  │                                      │
                  │                              Fix PR → Human Review
                  │                                      │
                  └──────────────── CI/CD ◄──── Merge ───┘
```
