FROM python:3.12-alpine

WORKDIR /app
RUN pip install --no-cache-dir flask requests

# Copy only what's needed (script + .env baked in is NOT recommended —
# keep secrets out of the image; mount or pass env instead)
COPY dm.py .
COPY config.py .

EXPOSE 7860
CMD ["python", "dm.py", "--web"]
