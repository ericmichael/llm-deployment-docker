# Use image with both Python 3.10 and Node.js 18 pre-installed
FROM nikolaik/python-nodejs:python3.10-nodejs18-slim

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

# Install Python dependencies (system-wide, so root can use them at runtime)
COPY requirements.txt ./
RUN pip install --no-cache-dir --upgrade -r requirements.txt

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
EXPOSE 4000

# Health check for container orchestration
HEALTHCHECK --interval=30s --timeout=10s --start-period=120s --retries=3 \
    CMD curl -f http://localhost:8000/health/ || exit 1

# Run both LiteLLM and Django when the container launches
CMD ["/start.sh"]
