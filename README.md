# Agentic DevOps & SRE Demo

A fully automated DevOps pipeline demonstrating the **self-healing development loop**: Azure SRE Agent detects production incidents, creates rich GitHub Issues, GitHub Actions triages and assigns the Copilot Coding Agent, which implements fixes and opens PRs — all without human intervention until the review stage.

## 🔄 The Agentic Loop

```
Developer pushes code → GitHub Actions CI/CD deploys to Azure App Service →
Azure SRE Agent monitors (App Insights + PostgreSQL) →
SRE Agent detects incident → Creates rich GitHub Issue →
GitHub Actions triages & assigns Copilot Coding Agent →
Coding Agent creates fix PR → Human reviews & approves →
CI/CD redeploys → SRE Agent confirms resolution → Loop continues
```

## 🏗️ Architecture

```
┌─────────────┐    push/merge     ┌──────────────────┐    deploy    ┌──────────────────────────────┐
│  Developer   │ ───────────────► │  GitHub Actions   │ ──────────► │  Azure App Service (Linux)   │
│  (Human)     │                  │  CI/CD Pipeline   │             │  Python 3.12 + FastAPI       │
└──────┬───────┘                  └──────────────────┘             │  + Azure PostgreSQL (Std)    │
       │                                   ▲                       │  + Application Insights      │
       │ review PR                         │ merge                 └────────────┬─────────────────┘
       │                                   │                                    │
┌──────▼───────┐    creates PR    ┌────────┴─────────┐  assigns   ┌────────────▼─────────────────┐
│  Pull Request │ ◄────────────── │ Copilot Coding   │ ◄───────── │  Azure SRE Agent             │
│  (for review) │                 │ Agent            │  via GH    │  → detects incidents         │
└──────────────┘                  └──────────────────┘  Actions   │  → creates GitHub Issues     │
                                                                   └──────────────────────────────┘
```

## 🛠️ Tech Stack

| Component | Technology |
|-----------|-----------|
| Web Framework | FastAPI (Python 3.12) |
| Database | Azure Database for PostgreSQL (Flexible Server) |
| ORM | SQLAlchemy 2.0 (async) + Alembic |
| Monitoring | Azure Application Insights + Log Analytics |
| CI/CD | GitHub Actions |
| Infrastructure | Azure Bicep |
| Incident Detection | Azure SRE Agent |
| Auto-Fix | GitHub Copilot Coding Agent |

## 📂 Project Structure

```
├── src/                          # FastAPI application
│   ├── main.py                   # App entry point
│   ├── config.py                 # Pydantic settings
│   ├── database.py               # Async SQLAlchemy engine
│   ├── models.py                 # ORM models
│   ├── schemas.py                # Pydantic schemas
│   ├── routes/                   # API route handlers
│   │   ├── tasks.py              # Task CRUD
│   │   └── health.py             # Health check
│   └── middleware/               # Middleware
│       ├── error_handler.py      # Global exception handler
│       └── logging_middleware.py  # Request logging
├── tests/                        # Pytest test suite
├── infrastructure/               # Azure Bicep IaC
│   ├── main.bicep                # All Azure resources
│   ├── parameters.json           # Deployment parameters
│   └── deploy.sh                 # Deployment helper script
├── demo/                         # Demo scripts
│   ├── generate_traffic.sh       # Trigger the demo bug
│   └── README.md                 # Presenter's guide
├── docs/                         # Documentation
│   ├── architecture.md           # Architecture deep-dive
│   ├── azure-setup.md            # Azure provisioning guide
│   └── sre-agent-setup.md        # SRE Agent configuration
├── .github/
│   ├── workflows/
│   │   ├── ci.yml                # CI: lint, test, security
│   │   ├── cd.yml                # CD: deploy to Azure
│   │   └── sre-issue-triage.yml  # Auto-triage SRE issues
│   ├── copilot-instructions.md   # Coding agent guidelines
│   └── ISSUE_TEMPLATE/
│       └── sre-incident.yml      # SRE incident template
├── alembic/                      # Database migrations
├── Dockerfile                    # Container image
├── requirements.txt              # Production dependencies
├── requirements-dev.txt          # Dev/test dependencies
└── pyproject.toml                # Project config
```

## 🚀 Quick Start

### Prerequisites
- Python 3.12+
- PostgreSQL (local or Azure)
- Azure CLI (for deployment)

### Local Development

```bash
# Clone the repo
git clone https://github.com/hanwesh/agentic-devops-sre-demo.git
cd agentic-devops-sre-demo

# Create virtual environment
python -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements-dev.txt

# Set environment variables (or create .env file)
export DATABASE_URL="postgresql+asyncpg://postgres:postgres@localhost:5432/tasksdb"

# Run database migrations
alembic upgrade head

# Start the application
uvicorn src.main:app --reload

# Run tests
pytest -v --cov=src

# Run linter
ruff check src/ tests/
```

### Deploy to Azure

See the [Azure Setup Guide](docs/azure-setup.md) for detailed instructions.

```bash
# Quick deploy
chmod +x infrastructure/deploy.sh
./infrastructure/deploy.sh my-resource-group eastus
```

## 🎯 API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Welcome message + links |
| `GET` | `/health` | Health check (DB, uptime, version) |
| `GET` | `/docs` | Swagger UI |
| `GET` | `/api/tasks` | List tasks (paginated) |
| `POST` | `/api/tasks` | Create a task |
| `GET` | `/api/tasks/{id}` | Get a task |
| `PUT` | `/api/tasks/{id}` | Update a task |
| `DELETE` | `/api/tasks/{id}` | Delete a task |

### Demo Bug Trigger
```
GET /api/tasks?filter=broken → 500 Internal Server Error
```
This intentional bug path is used to demonstrate the SRE incident detection loop.

## 🎬 Running the Demo

See the full [Presenter's Guide](demo/README.md) for a scripted walkthrough.

Quick version:
1. Deploy the app and verify it's healthy
2. Run `./demo/generate_traffic.sh https://<app>.azurewebsites.net`
3. Watch Azure SRE Agent create a GitHub Issue
4. Watch the triage workflow assign Copilot Coding Agent
5. Watch Copilot create a fix PR
6. Review, approve, merge — app self-heals

## 📖 Documentation

- [Architecture](docs/architecture.md) — System design and data flows
- [Azure Setup](docs/azure-setup.md) — Infrastructure provisioning guide
- [SRE Agent Setup](docs/sre-agent-setup.md) — SRE Agent configuration
- [Demo Guide](demo/README.md) — Step-by-step presenter's guide

## License

MIT