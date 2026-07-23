# Base image with Python already set up
FROM python:3.11-slim

# Install Node.js + npm (needed for gmgn-cli, which bot_v5.py calls as a
# subprocess) and curl (used to fetch Node's setup script)
RUN apt-get update && \
    apt-get install -y curl && \
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y nodejs && \
    rm -rf /var/lib/apt/lists/*

# Install gmgn-cli globally (only its read-only market/trending data is
# used — see the security note in chat about not configuring
# GMGN_PRIVATE_KEY with this tool)
RUN npm install -g gmgn-cli

WORKDIR /app

# Install Python dependencies first (better Docker layer caching — this
# layer only rebuilds when requirements.txt changes, not on every code edit)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Now copy the actual bot files
COPY bot_v5.py .
COPY meta_categories.txt .
COPY trending_lore.txt .

CMD ["python3", "bot_v5.py"]
