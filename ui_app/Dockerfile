FROM python:3.9-slim

WORKDIR /app

RUN pip install --no-cache-dir flask

COPY app.py /app/app.py
COPY templates /app/templates

CMD ["python", "-u", "/app/app.py"]
