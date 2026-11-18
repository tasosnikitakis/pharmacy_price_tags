# Core image: FastAPI app plus Alembic migrations. The gateway runs natively (systemd).
FROM python:3.12-slim

RUN pip install --no-cache-dir "uv>=0.11,<0.12"

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH" PYTHONUNBUFFERED=1

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-install-project --extra core

COPY alembic.ini ./
COPY core ./core
COPY shared ./shared
COPY gateway ./gateway
RUN uv sync --frozen --extra core

RUN useradd --system --uid 10001 esl
USER esl

EXPOSE 8000
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn core.app.main:app --host 0.0.0.0 --port 8000"]
