FROM python:3.12-slim

WORKDIR /app

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Copy dependency files first for layer caching
COPY pyproject.toml README.md ./
COPY src/ src/

# Install into the system Python (no venv needed inside the container)
RUN uv pip install --system --no-cache .

# Run via the installed entry point
CMD ["discordagent"]
