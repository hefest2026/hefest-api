# Development image for hefest-api.
#
# Source and venv are volume-mounted by compose for hot-reload; this image only
# needs to seed the venv (named volume) and run uvicorn with --reload.

FROM python:3.12-slim

# uv as a pinned binary instead of `pip install uv` — smaller and faster.
COPY --from=ghcr.io/astral-sh/uv:0.11.21 /uv /uvx /bin/

# UV_COMPILE_BYTECODE: precompile .pyc for faster startup.
# UV_LINK_MODE=copy: silence cross-filesystem hardlink warnings with the cache mount.
# UV_PYTHON_DOWNLOADS=0: use the base image's system Python, never fetch one.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0

WORKDIR /app

# Install all deps (including the dev group) in a layer keyed only on the
# lockfiles, bind-mounted so a source change doesn't bust this cache.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --all-groups --no-install-project

# Source is mounted as a volume in dev; copy here so the image is usable standalone.
COPY . .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --all-groups

RUN chmod +x /app/docker-entrypoint.sh

EXPOSE 8000

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["uv", "run", "uvicorn", "hefest.main:app", "--host", "0.0.0.0", "--port", "8000"]
