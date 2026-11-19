FROM python:3.12-alpine

WORKDIR /app
RUN pip install --no-cache-dir flask requests

COPY dm.py config.py analyst.py arc.py ./
COPY styles/ ./styles/
COPY web/ ./web/

EXPOSE 7860
CMD ["python", "dm.py", "--web"]
