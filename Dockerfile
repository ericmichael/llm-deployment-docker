# Use an official Python runtime as a parent image
FROM python:3.10-slim-bookworm

# Set environment variables
ENV APP_HOME=/usr/src/app \
	PATH=/home/user/.local/bin:$PATH

RUN apt-get -y update

# Set working directory in the container
WORKDIR $APP_HOME

# Ensure node.js 18 is available for apt-get
ARG NODE_MAJOR=18
RUN curl -sL https://deb.nodesource.com/setup_$NODE_MAJOR.x | bash -

# Install any needed packages
RUN apt-get -y update && \
    apt-get install -y nodejs npm && \
    rm -rf /var/lib/apt/lists/*

# Copy only the requirements.txt first, for better cache on builds
COPY ./requirements.txt ./

# Install Python dependencies
RUN pip install --no-cache-dir --upgrade -r requirements.txt

# Create a user to run the app
RUN useradd -m -u 1000 user

# Change the ownership of the .cache directory
RUN mkdir -p $HOME/.cache && chown -R user:user $HOME/.cache

# Copy the entrypoint script into the container
COPY entrypoint.sh /entrypoint.sh

# Make the entrypoint script executable
RUN chmod +x /entrypoint.sh

# Copy the current directory contents into the container at /app
COPY --chown=user . $APP_HOME/

# Install Node dependencies
RUN npm install

# Run the Webpacker build
RUN npm run build

USER user

# Make port 8000 available to the world outside this container
EXPOSE 8000

# Set the entrypoint script as the entrypoint
ENTRYPOINT ["/entrypoint.sh"]

# Run the application when the container launches
# Run the Gunicorn server when the container launches
CMD ["gunicorn", "aistarterkit.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3"]