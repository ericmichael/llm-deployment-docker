# Use image with both Python 3.10 and Node.js 18 pre-installed
FROM nikolaik/python-nodejs:python3.10-nodejs18-slim

# Set environment variables
ENV APP_HOME=/usr/src/app \
	PATH=/home/pn/.local/bin:$PATH

# Set working directory in the container
WORKDIR $APP_HOME

# Copy only the requirements.txt first, for better cache on builds
COPY requirements.txt ./

# Install Python dependencies
RUN pip install --no-cache-dir --upgrade -r requirements.txt

# Apply patches to litellm (unmerged fixes for Azure realtime WebSocket)
COPY patches/ /tmp/patches/
RUN cp /tmp/patches/litellm_azure_realtime.py /usr/local/lib/python3.10/site-packages/litellm/llms/azure/realtime/handler.py

# Copy package files for Node dependencies
COPY package*.json ./

# Install Node dependencies
RUN npm install

# Copy the entrypoint script into the container
COPY entrypoint.sh /entrypoint.sh

# Make the entrypoint script executable
RUN chmod +x /entrypoint.sh

# Copy the start script into the container
COPY start.sh /start.sh

# Make the start script executable
RUN chmod +x /start.sh

# Copy the current directory contents into the container
COPY --chown=pn:pn . $APP_HOME/

# Run the Webpacker build
RUN npm run build

# Change the ownership of the .cache directory
RUN mkdir -p /home/pn/.cache && chown -R pn:pn /home/pn/.cache

# Switch to non-root user
USER pn

# Make ports available to the world outside this container
EXPOSE 8000
EXPOSE 4000

# Run both LiteLLM and Django when the container launches
CMD ["/start.sh"]