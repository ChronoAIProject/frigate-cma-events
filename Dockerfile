FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY --from=ghcr.io/astral-sh/uv:0.9.5 /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
COPY aggregation.py config.py service.py evidence.py ./
RUN mkdir /data && chown 1000:1000 /data
USER 1000:1000
EXPOSE 8080
CMD ["/app/.venv/bin/uvicorn", "service:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
