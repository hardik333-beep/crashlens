# Dashboard build image: compiles the Vite React app to static files.
# The compiled output in /app/dist is copied into a shared volume by the
# one-shot "dashboard" compose service so caddy can serve it.
# Build context is the repository root.
FROM node:22-alpine AS build

WORKDIR /app

COPY dashboard/package.json dashboard/package-lock.json ./
RUN npm ci

COPY dashboard/ ./
RUN npm run build

# Minimal final stage that just carries the built assets.
FROM alpine:3

WORKDIR /app
COPY --from=build /app/dist ./dist
