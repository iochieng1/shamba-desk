FROM node:22.11.0-bookworm-slim

COPY --from=ghcr.io/astral-sh/uv:0.8.22 /uv /uvx /bin/
ENV UV_LINK_MODE=copy UV_PYTHON_INSTALL_DIR=/opt/python PYTHONUNBUFFERED=1

WORKDIR /app
# Cache the borrowed Open-Meteo MCP server so runs do not download it.
RUN timeout 60 npx -y open-meteo-mcp-server@2.4.2 </dev/null >/dev/null 2>&1 || true

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
COPY data ./data
COPY evals ./evals
RUN uv sync --frozen --no-dev

ENTRYPOINT ["uv", "run", "--no-sync", "shamba-desk"]
CMD ["run", "CL-KHA"]
