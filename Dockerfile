FROM python:3.12-slim
ARG PIP_INDEX_URL=https://pypi.org/simple
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 MCPT_HOME=/data
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home mcpt
WORKDIR /app
COPY pyproject.toml requirements.lock ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/pip python -m pip install --retries 5 --timeout 60 --index-url "$PIP_INDEX_URL" -c requirements.lock setuptools wheel \
    && python -m pip install --retries 5 --timeout 60 --index-url "$PIP_INDEX_URL" --no-build-isolation -c requirements.lock .
RUN mkdir /data && chown mcpt:mcpt /data
USER mcpt
ENTRYPOINT ["python", "-m", "mcp_transfer", "--home", "/data"]
CMD ["cloud"]
