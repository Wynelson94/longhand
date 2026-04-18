FROM python:3.12-slim

RUN pip install --no-cache-dir longhand==0.5.11

ENTRYPOINT ["longhand", "mcp-server"]
