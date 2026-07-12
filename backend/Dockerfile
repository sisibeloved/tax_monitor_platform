FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:${PATH}"

RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --upgrade pip setuptools

WORKDIR /app

COPY pyproject.toml ./
COPY requirements.lock ./
COPY src ./src
RUN pip install --no-cache-dir --constraint requirements.lock .

COPY alembic.ini ./
COPY migrations ./migrations

RUN addgroup --system taxrisk \
    && adduser --system --ingroup taxrisk taxrisk \
    && chown -R taxrisk:taxrisk /app

USER taxrisk

EXPOSE 8000

CMD ["uvicorn", "tax_risk.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
