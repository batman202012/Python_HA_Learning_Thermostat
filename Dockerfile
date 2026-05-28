# Use a slim Python runtime for small image sizes
FROM python:3.11-slim

# Enable unbuffered logging and establish explicit root module search paths
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

# Establish our working directory inside the container
WORKDIR /app

# Install system dependencies if required
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Copy only requirements first to exploit Docker layer caching
COPY requirements.txt .

# Install dependencies cleanly without caching installation packages
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application files into the image
COPY . .

# Pre-compile the entire workspace tree into optimized bytecode files
RUN python -m compileall .

# Expose the FastAPI web dashboard port
EXPOSE 3000

# Start the application using module-string notation from the root directory context
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "3000"]