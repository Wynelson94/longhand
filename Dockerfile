FROM python:3.12-slim

RUN pip install --no-cache-dir longhand==0.9.4

ENTRYPOINT ["longhand", "mcp-server"]
