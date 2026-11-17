# syntax=docker/dockerfile:1

FROM node:24-alpine AS base
WORKDIR /app
COPY package.json package-lock.json ./

# --- development : serveur Vite avec rechargement, sources montées en volume ---
FROM base AS development
RUN npm ci
COPY . .
EXPOSE 5173
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0", "--port", "5173"]

# --- builder : bundle de production -------------------------------------------
FROM base AS builder
RUN npm ci
COPY . .
RUN npm run build

# --- production : fichiers statiques servis par nginx, proxy vers l'API --------
FROM nginx:1.29-alpine AS production
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=builder /app/dist /usr/share/nginx/html
EXPOSE 80
