FROM python:3.11-slim
ENV PYTHONUNBUFFERED=1
WORKDIR /app
COPY speed-tester/tester.py speed-tester/history.py ./
COPY common/__init__.py ./common/__init__.py
COPY common/logutil.py ./common/logutil.py
CMD ["python", "tester.py"]
