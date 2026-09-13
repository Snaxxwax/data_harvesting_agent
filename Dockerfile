FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HARVEST_DB=/data/harvest.sqlite
RUN pip install --no-cache-dir uv==0.12.11
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable && \
    groupadd --gid 10001 harvest && useradd --uid 10001 --gid 10001 --no-create-home harvest && \
    mkdir /data && chown harvest:harvest /data
ENV PATH="/app/.venv/bin:$PATH"
USER 10001:10001
VOLUME ["/data"]
EXPOSE 8000
CMD ["harvest", "serve", "--host", "0.0.0.0"]
