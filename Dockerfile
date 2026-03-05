# Use an official Python runtime as a parent image
FROM python:3.12-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1
ENV PORT 8000

# Set work directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    build-essential \
    libpq-dev \
    curl \
    --no-install-recommends && \
    rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

# Copy project
COPY . /app/

# Create a non-root user and switch to it
RUN useradd -m myuser && chown -R myuser:myuser /app

# Switch to root to ensure scripts are executable and collectstatic works
USER root
RUN chmod +x /app/start.sh /app/release.sh

# Run collectstatic during build as myuser to avoid doing it on every boot
# We set a dummy SECRET_KEY to avoid decoupling issues if not provided
USER myuser
RUN SECRET_KEY=build-time-only-secret python manage.py collectstatic --noinput

# Expose port
EXPOSE 8000

# Run the application
CMD ["/app/start.sh"]
