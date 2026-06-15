FROM python:3.14-slim AS base
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Agent-runtime toolchain (NOT needed by the SDK itself — it bundles its own CLI):
#   git           — the `claude plugin marketplace add` clone + GitHub working +
#                   private plugin sources (proven required: the marketplace add
#                   shells out to `git clone` and fails without it).
#   ca-certificates — TLS trust for the HTTPS git clone.
#   nodejs + npm  — provide `npx` so npx-based MCP servers (e.g. the GitHub MCP
#                   server and many official servers) can launch. uvx-based MCP
#                   servers already work via the baked `uv`/`uvx`.
# Kept lean: --no-install-recommends, apt lists removed in the same layer.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        git \
        ca-certificates \
        nodejs \
        npm \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /code/
COPY pyproject.toml uv.lock ./

ENV UV_PROJECT_ENVIRONMENT="/usr/local/"
RUN uv sync --no-dev --frozen

COPY src/ src/
COPY scripts/ scripts/

FROM base AS test
RUN uv sync --all-groups --frozen
COPY tests/ tests/
RUN uv run ruff check src/ tests/
CMD ["uv", "run", "pytest", "tests/", "-v"]

FROM base AS production
CMD ["python", "-u", "/code/src/component.py"]
