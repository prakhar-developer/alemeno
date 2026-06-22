# Use lightweight official Python image
FROM python:3.12-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV WORKDIR=/workspace

# Set working directory
WORKDIR $WORKDIR

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy and install python dependencies
COPY requirements.txt $WORKDIR/
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source code
COPY app/ $WORKDIR/app/

# Copy test suite (allows running `docker exec ... pytest tests/` for verification)
COPY tests/ $WORKDIR/tests/

# Create uploads directory in container
RUN mkdir -p $WORKDIR/uploads

# Expose port 8000 for FastAPI
EXPOSE 8000

# Default command (will be overridden in docker-compose for celery worker)
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
