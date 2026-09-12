# Frontend: the Next.js chat client.
#
#   docker build --build-arg NEXT_PUBLIC_API_BASE=https://api.example.com \
#                -t copilot-frontend ./frontend
#
# Read the NEXT_PUBLIC_API_BASE section below before deploying. It is the one
# thing about this image that does not behave the way a server config normally
# does, and getting it wrong produces a frontend that loads perfectly and then
# fails every request.

# ─────────────────────────────────────────────────────────────────────────────
# 1. Dependencies
# ─────────────────────────────────────────────────────────────────────────────
FROM node:22-alpine AS deps

WORKDIR /app

# Only the manifests, so this layer is reused until a dependency actually
# changes. `npm ci` rather than `npm install`: it installs exactly the lockfile
# and fails if the two disagree, instead of quietly resolving something newer.
COPY package.json package-lock.json ./
RUN npm ci

# ─────────────────────────────────────────────────────────────────────────────
# 2. Build
# ─────────────────────────────────────────────────────────────────────────────
FROM node:22-alpine AS builder

WORKDIR /app

COPY --from=deps /app/node_modules ./node_modules
COPY . .

# ── NEXT_PUBLIC_API_BASE is baked in here, at build time ────────────────────
#
# Every fetch in this app runs in the browser: all of src/app is "use client"
# and src/lib/api.ts is the only thing that calls the API. Next replaces
# process.env.NEXT_PUBLIC_* with a literal string during the build, so this
# value is compiled into the JavaScript the browser downloads.
#
# Two consequences that bite in exactly this deployment:
#
#   1. It must be a URL the *user's browser* can resolve. Not `http://backend:8000`
#      -- that name exists only on the compose network, and the browser is not on
#      it. On a public VM it is `http://<public-ip>:8000`; on RunPod it is the
#      proxy hostname for port 8000.
#   2. Setting it in compose `environment:` at run time does nothing. The string
#      is already compiled in. Changing it means rebuilding this image.
#
# The default is the local dev value, which matches src/lib/api.ts's own
# fallback, so an argument-less build still works on a laptop.
#
# Not shadowed by the developer's .env.local, on two counts: .dockerignore keeps
# it out of the build context, and Next resolves process.env ahead of every .env
# file anyway. Worth knowing, because .env.local in this repo sets this variable
# to 127.0.0.1 and would otherwise be a silent way to ship a broken client.
#
# No default value, deliberately. An unset ARG is the empty string, and
# next.config.ts refuses to build on that -- where a localhost default would
# quietly succeed and produce exactly the broken client described above.
ARG NEXT_PUBLIC_API_BASE
ENV NEXT_PUBLIC_API_BASE=$NEXT_PUBLIC_API_BASE

# The opt-in for deliberately building a container to run on this machine.
# next.config.ts fails the build on a loopback address without it.
ARG ALLOW_LOCALHOST_API_BASE
ENV ALLOW_LOCALHOST_API_BASE=$ALLOW_LOCALHOST_API_BASE

ENV NEXT_TELEMETRY_DISABLED=1

# next.config.ts asserts NEXT_PUBLIC_API_BASE is reachable before this compiles
# anything, so a misconfigured build fails here with an explanation rather than
# succeeding and failing silently in someone's browser later.
RUN npm run build

# ─────────────────────────────────────────────────────────────────────────────
# 3. Runtime
#
# Carries the standalone output and nothing else -- no source, no toolchain, no
# dev dependencies.
# ─────────────────────────────────────────────────────────────────────────────
FROM node:22-alpine AS runner

WORKDIR /app

ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0

# HOSTNAME=0.0.0.0 is required, not cosmetic: the standalone server binds
# localhost by default, which inside a container means nothing outside it can
# connect and the port mapping appears broken.

# `node` (uid 1000) ships with the base image. Same reasoning as the backend:
# nothing here needs root.
COPY --from=builder --chown=node:node /app/.next/standalone ./
COPY --from=builder --chown=node:node /app/.next/static ./.next/static
COPY --from=builder --chown=node:node /app/public ./public

USER node

EXPOSE 3000

# Whether the Next server is serving, nothing more. It has no upstream to check:
# the browser talks to the API directly, so this container being healthy says
# nothing about the backend and should not.
HEALTHCHECK --interval=15s --timeout=5s --start-period=15s --retries=3 \
    CMD node -e "require('http').get('http://127.0.0.1:3000/',r=>process.exit(r.statusCode<500?0:1)).on('error',()=>process.exit(1))"

CMD ["node", "server.js"]
