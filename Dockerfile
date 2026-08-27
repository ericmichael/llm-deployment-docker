# Use image with both Python 3.12 and Node.js 20 pre-installed (LiteLLM needs Python >= 3.11)
FROM nikolaik/python-nodejs:python3.12-nodejs20-slim

# Set environment variables
ENV APP_HOME=/usr/src/app

# Set working directory in the container
WORKDIR $APP_HOME

# === ROOT OPERATIONS ===
USER root

# Install PostgreSQL client libraries
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    postgresql-client \
    && rm -rf /var/lib/apt/lists/*

# Copy scripts and make executable (as root)
COPY entrypoint.sh /entrypoint.sh
COPY start.sh /start.sh
RUN chmod +x /entrypoint.sh /start.sh

# Set ownership of app directory
RUN chown -R pn:pn $APP_HOME

RUN mkdir -p /var/lib/litellm/ui /var/lib/litellm/assets && chown -R pn:pn /var/lib/litellm

# Install Python dependencies (system-wide, so root can use them at runtime)
COPY requirements.txt ./
RUN pip install --no-cache-dir --upgrade -r requirements.txt

# Prisma resolves its engine cache from $HOME; build as root but cache to a
# shared path so the runtime user (pn) doesn't re-download engines at boot.
ENV PRISMA_BINARY_CACHE_DIR=/var/lib/prisma-cache
RUN mkdir -p $PRISMA_BINARY_CACHE_DIR \
    && prisma generate --schema $(python -c "import pathlib, litellm.proxy; print(pathlib.Path(litellm.proxy.__file__).parent / 'schema.prisma')") \
    && chown -R pn:pn $PRISMA_BINARY_CACHE_DIR

# === USER OPERATIONS ===
USER pn

# Copy and install Node dependencies
COPY --chown=pn:pn package*.json ./
RUN npm install

# Copy the rest of the application
COPY --chown=pn:pn . $APP_HOME/

# Run the Webpacker build
RUN npm run build

# Make ports available to the world outside this container
EXPOSE 8000

# Health check for container orchestration
HEALTHCHECK --interval=30s --timeout=10s --start-period=120s --retries=3 \
    CMD curl -f http://localhost:8000/health/ || exit 1

# Entrypoint runs migrations, sets up LiteLLM DB, collectstatic
ENTRYPOINT ["/entrypoint.sh"]

# Run both LiteLLM and Django when the container launches
CMD ["/start.sh"]
