FROM python:3.12-slim

WORKDIR /app

RUN pip install uv --no-cache-dir

COPY pyproject.toml uv.lock ./

# Install all deps (including dev) into the project venv
RUN uv sync --frozen --all-groups

# Source is mounted as a volume in dev; copy here for production builds
COPY . .

EXPOSE 8000

CMD ["uv", "run", "uvicorn", "hefest.main:app", "--host", "0.0.0.0", "--port", "8000"]
