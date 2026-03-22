# Copilot Coding Agent Instructions

## Project Context
This is a **FastAPI** application (Python 3.12) backed by **Azure Database for PostgreSQL**.
It serves as a demo for the Agentic DevOps & SRE loop.

## Code Style
- Follow **PEP 8** and use **type hints** on all function signatures
- Use `ruff` for linting and formatting (configured in `pyproject.toml`)
- Max line length: 88 characters (ruff default)
- Use `async`/`await` for all database operations
- Use SQLAlchemy 2.0 style queries (`select()`, not legacy `Query`)

## Architecture
- **Entry point**: `src/main.py` — FastAPI application factory
- **Config**: `src/config.py` — Pydantic Settings from env vars
- **Database**: `src/database.py` — Async SQLAlchemy engine/sessions
- **Models**: `src/models.py` — SQLAlchemy ORM models
- **Schemas**: `src/schemas.py` — Pydantic request/response schemas
- **Routes**: `src/routes/` — FastAPI route handlers
- **Middleware**: `src/middleware/` — Error handling, logging
- **Tests**: `tests/` — Pytest + httpx async tests

## When Fixing SRE Issues
1. Read the **Stack Trace** section to find the exact file and line number
2. Read the **Root Cause Analysis** for context on what went wrong
3. Read the **Suggested Fix** for guidance (but verify it's correct)
4. Implement the fix in the relevant file(s)
5. **Always** add or update tests to cover the fix
6. Run linting and tests:
   ```bash
   ruff check src/ tests/
   pytest -v
   ```
7. Create a PR with title: `fix: <description>` referencing `Fixes #<issue-number>`

## Database Patterns
- Always use dependency injection for DB sessions: `db: AsyncSession = Depends(get_db)`
- Use `await db.flush()` + `await db.refresh(obj)` after mutations
- Handle `None` results explicitly to avoid `NoneType` errors
- Use `select()` + `where()` for queries

## Testing Patterns
- Use `pytest.mark.asyncio` on all async tests
- Tests use an in-memory SQLite database (see `tests/conftest.py`)
- Use the `client` fixture for HTTP-level tests
- Assert both status codes and response body content

## Commands
- **Lint**: `ruff check src/ tests/`
- **Format**: `ruff format src/ tests/`
- **Type check**: `mypy src/ --ignore-missing-imports`
- **Test**: `pytest -v --cov=src`
