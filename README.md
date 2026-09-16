# Data Cleansing Agent Backend

FastAPI backend for the UOM data-cleansing agent. It validates existing standardized values, applies version-controlled deterministic unit rules, selectively uses Google ADK/Gemini for unresolved description evidence, and exports the updated workbook.

## Local setup

Requires Python 3.12 and MongoDB.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[ai,test]'
cp .env.example .env
python -m uvicorn app.main:app --reload --port 8000
```

The API documentation is available at `http://localhost:8000/docs` and the health endpoint at `http://localhost:8000/health`.

The default `AI_PROVIDER=mock` makes no external model calls. To use Google ADK with the Gemini Developer API, set `AI_PROVIDER=adk`, `GEMINI_API_KEY`, and `GEMINI_MODEL` in the local `.env`. Never commit that file.

## Tests

```bash
source .venv/bin/activate
PYTHONPATH=tests/test_bootstrap python -m pytest
```

The `PYTHONPATH` entry activates the repository's small macOS/Python 3.12 readline test bootstrap; it is harmless on other platforms.

Deterministic unit mappings are maintained in `app/rules/unit_mappings.v1.yaml`. See `app/rules/README.md` and `app/agents/README.md` for ownership and maintenance guidance.
