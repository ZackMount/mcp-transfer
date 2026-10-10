FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 MCPT_HOME=/data
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home mcpt
WORKDIR /app
COPY pyproject.toml requirements.lock ./
COPY src ./src
RUN pip install --no-cache-dir -c requirements.lock .
RUN mkdir /data && chown mcpt:mcpt /data
USER mcpt
ENTRYPOINT ["python", "-m", "mcp_transfer", "--home", "/data"]
CMD ["cloud"]
