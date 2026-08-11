FROM python:3.9-slim
WORKDIR /app
RUN pip install --no-cache-dir pyserial pyModbusTCP
COPY io_control.py /app/io_control.py
CMD ["python", "-u", "/app/io_control.py"]