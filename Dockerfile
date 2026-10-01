# Image for the project's Django code, used by the one-shot `init` service in
# compose.yaml that prepares a fresh database. The schema and the board data
# live in the volume, not in this image, so the container exits once it is done.

FROM ghcr.io/astral-sh/uv:python3.14-trixie-slim

# Install into the image's own Python rather than a .venv, so `python manage.py`
# works without an activation step. Bytecode is compiled ahead of time because
# the container is short-lived and starts more often than it is built.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/usr/local \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependencies resolve from the lock file alone, so this layer is cached across
# source edits. The project is virtual (no build backend), hence deps only.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    uv sync --locked --no-install-project

COPY . .

CMD ["python", "manage.py", "check"]
