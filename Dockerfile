FROM python:3.12-slim

RUN pip install --no-cache-dir longhand==0.9.3

ENTRYPOINT ["longhand", "mcp-server"]
