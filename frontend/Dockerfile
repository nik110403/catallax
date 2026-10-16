# ── Build stage ──────────────────────────────────────────────
FROM node:22-alpine AS build

WORKDIR /srv

COPY package.json package-lock.json ./
RUN npm ci

COPY . .

RUN npm run build

# ── Serve stage ──────────────────────────────────────────────
FROM nginx:1.27-alpine AS serve

COPY --from=build /srv/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80
