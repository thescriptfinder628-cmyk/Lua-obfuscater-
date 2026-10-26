# Forsakenis4 Lua Obfuscator — single container, no build step needed.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000

WORKDIR /app

# install the app first so the layer is cached between code changes
COPY pyproject.toml README.md LICENSE ./
COPY luaobf ./luaobf
RUN pip install --no-cache-dir . && pip install --no-cache-dir lupa

# the web front end and the server
COPY tools ./tools
COPY playground ./playground

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+__import__('os').environ.get('PORT','8000')+'/healthz').status==200 else 1)"

CMD ["python", "tools/playground.py"]
