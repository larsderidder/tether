FROM node:20-slim

WORKDIR /app

# Copy SDK source and install its dependencies
COPY codex-src/sdk/typescript /codex-src/sdk/typescript
WORKDIR /codex-src/sdk/typescript
RUN npm install --ignore-scripts

# Back to sidecar directory
WORKDIR /app

# Copy sidecar package files and install deps
COPY codex-sdk-sidecar/package*.json ./
RUN npm ci

# Install Codex CLI so the SDK can spawn `codex` inside the container.
# Note: if you prefer to use a host-installed codex binary, run the sidecar
# natively and set TETHER_CODEX_SIDECAR_CODEX_BIN accordingly.
RUN npm install -g @openai/codex@0.98.0

# Copy sidecar source
COPY codex-sdk-sidecar/src ./src
COPY codex-sdk-sidecar/tsconfig.json ./

ENV TETHER_CODEX_SIDECAR_HOST=0.0.0.0
ENV TETHER_CODEX_SIDECAR_PORT=8788

EXPOSE 8788

CMD ["npm", "run", "start"]
