FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install -r requirements.txt

# Copy app code
COPY . .

# Decode Google credentials at build/runtime via entrypoint
COPY start.sh .
RUN chmod +x start.sh

# Use Railway's dynamic PORT
CMD ["./start.sh"]