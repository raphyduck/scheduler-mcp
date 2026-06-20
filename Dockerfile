FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY scheduler_mcp ./scheduler_mcp
ENV PYTHONUNBUFFERED=1
CMD ["python", "-m", "scheduler_mcp.main"]
