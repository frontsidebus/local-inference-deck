# Walter digest - small Starlette/uvicorn app (on-demand digest runs + history). Base pinned by digest (python:3.13-alpine).
ARG BASE=python:3.13-alpine@sha256:79e7a9b9ff1cbceff819f856fb374477792a5967759d94df266de7b7b4120e6f

FROM ${BASE} AS build
COPY requirements.txt /tmp/requirements.txt
RUN python -m venv /venv \
 && /venv/bin/pip install --no-cache-dir --disable-pip-version-check -r /tmp/requirements.txt \
 && find /venv -name '__pycache__' -prune -exec rm -rf {} + \
 && rm -rf /venv/lib/python3.13/site-packages/pip* /venv/bin/pip*

FROM ${BASE}
ENV PATH=/venv/bin:$PATH PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY --from=build /venv /venv
COPY app/ /app/
RUN addgroup -S -g 10001 digest && adduser -S -D -H -u 10001 -G digest digest \
 && python -m compileall -q /app /venv/lib/python3.13/site-packages
USER 10001:10001
WORKDIR /app
EXPOSE 3300
CMD ["python", "-m", "main"]
