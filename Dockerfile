FROM python:3.12-alpine

WORKDIR /app
RUN addgroup -S dmd && adduser -S -u 1000 -G dmd dm
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./
COPY styles/ ./styles/
COPY web/ ./web/

RUN mkdir -p /app/campaigns && chown -R dm:dmd /app
USER dm
EXPOSE 7860
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:7860/healthz', timeout=3).read()" || exit 1
ENV DM_USE_WAITRESS=1 DM_HOST=0.0.0.0
CMD ["python", "dm.py", "--web"]
