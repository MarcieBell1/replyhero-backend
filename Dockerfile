FROM python:3.10-slim

# Install system packages
RUN apt-get update && apt-get install -y unrtf tesseract-ocr

# Set work directory
WORKDIR /app

# Copy project files
COPY . .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Expose port
EXPOSE 10000

# Start the app
CMD ["gunicorn", "main:app", "--bind", "0.0.0.0:10000"]