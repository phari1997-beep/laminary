# app/

**Owner:** `frontend` subagent

The Laminary client: one Expo (React Native + web) codebase for iOS, Android, and web, plus the statically rendered public story-shape pages.

Not scaffolded yet. When `frontend` runs `create-expo-app` here, CI picks it up automatically: the `app` job in `.github/workflows/ci.yml` does nothing until `app/package.json` exists, then runs `npm ci`, `npm run lint`, and `npm test` (the last two only if those scripts are defined). Commit `package-lock.json` so `npm ci` works.

Design rules: `docs/DESIGN.md`. Voice: `docs/VISION.md`.

Env vars the app will read (public values only, never the service-role key): `EXPO_PUBLIC_SUPABASE_URL`, `EXPO_PUBLIC_SUPABASE_ANON_KEY`, `EXPO_PUBLIC_POSTHOG_KEY`, `EXPO_PUBLIC_POSTHOG_HOST`. See `.env.example` and `docs/ENVIRONMENTS.md`.
