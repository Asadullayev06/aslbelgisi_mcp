FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py .

ENV MCP_HOST=0.0.0.0 MCP_PORT=8900
EXPOSE 8900
CMD ["python", "server.py"]
