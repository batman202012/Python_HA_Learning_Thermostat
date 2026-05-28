# Use a slim Python runtime for small image sizes
FROM python:3.11-slim

# Prevent Python from writing .pyc files and enable unbuffered logging
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Establish our working directory inside the container
WORKDIR /app

# Install system dependencies if required (sqlite3 is built into Python)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Copy only requirements first to exploit Docker layer caching
COPY requirements.txt .

# Install dependencies cleanly without caching installation packages
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application files into the image
COPY . .

# Force Python to compile the entry script and verify all underlying imports
RUN python -m py_compile main.py

# Expose the FastAPI web dashboard port
EXPOSE 3000

# Start the application pointing to main:app
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "3000"]