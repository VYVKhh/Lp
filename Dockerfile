FROM python:3.11-slim-bookworm

# Install build tools and dependencies for compiling Telegram Bot API
RUN apt-get update && apt-get install -y \
    git cmake g++ libssl-dev zlib1g-dev libevent-dev \
    autoconf automake libtool pkg-config curl wget unzip \
    && rm -rf /var/lib/apt/lists/*

# Clone Local Telegram Bot API Server Source
WORKDIR /src
RUN git clone --recursive --shallow-submodules --depth 1 https://github.com/tdlib/telegram-bot-api.git

# Build in a SEPARATE directory to avoid PreventInSourceBuild error
WORKDIR /src/build_dir
RUN cmake ../telegram-bot-api -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX:PATH=/usr/local && \
    cmake --build . --target install -- -j$(nproc)

# Setup Python Environment
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy Bot Code
COPY bot.py .

# Create data directory for shared storage
RUN mkdir -p /app/data

# Expose port for local API (internal use only)
EXPOSE 8081

# Entrypoint script to start both services
CMD ["sh", "-c", "telegram-bot-api --local --dir=/app/data --http-port=8081 --api-id=${TG_API_ID} --api-hash=${TG_API_HASH} & sleep 5 && python bot.py"]
