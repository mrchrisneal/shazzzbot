FROM python:3.14.10-slim@sha256:9aaa474097d094040649f2162a627c0049b408f52138d980916788d80f04802f AS preview
WORKDIR /src
COPY VERSION ./VERSION
COPY shazzzbot ./shazzzbot
COPY tools/preview_assets.py ./tools/preview_assets.py
COPY docs/demo/runtime.json ./docs/demo/runtime.json
RUN python -m tools.preview_assets --out /pyodide

FROM python:3.14.10-slim@sha256:9aaa474097d094040649f2162a627c0049b408f52138d980916788d80f04802f
RUN apt-get update && apt-get upgrade -y --no-install-recommends && rm -rf /var/lib/apt/lists/*
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY VERSION ./VERSION
COPY requirements.txt .
RUN pip install --no-cache-dir --require-hashes -r requirements.txt && pip uninstall -y pip
COPY shazzzbot ./shazzzbot
COPY assets/icon.png ./assets/icon.png
COPY docs/demo ./docs/demo
COPY --from=preview /pyodide ./docs/demo/pyodide
RUN useradd -r -u 10001 shazzzbot && mkdir /data && chown shazzzbot /data
USER shazzzbot
VOLUME /data
HEALTHCHECK --interval=60s --start-period=120s CMD ["python", "-m", "shazzzbot.health"]
CMD ["python", "-m", "shazzzbot"]
