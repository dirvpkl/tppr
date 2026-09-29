FROM python:3.12-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f

WORKDIR /app
COPY hash-gate/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY hash-gate/gate.py ./gate.py
COPY common/__init__.py ./common/__init__.py
COPY common/upstream.py ./common/upstream.py

CMD ["python", "gate.py"]
