FROM python:3.12-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f

WORKDIR /app
COPY post-prober/prober.py ./prober.py
COPY common/__init__.py ./common/__init__.py
COPY common/logutil.py ./common/logutil.py

CMD ["python", "prober.py"]
