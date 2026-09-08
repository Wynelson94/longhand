FROM python:3.12-slim

RUN pip install --no-cache-dir longhand==1.2.0

ENTRYPOINT ["longhand", "mcp-server"]
