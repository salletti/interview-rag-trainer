# Frontend — Interview RAG Trainer

React 19 + TypeScript strict + Vite. Tests avec Vitest et React Testing Library ; lint ESLint (règles typées), formatage Prettier.

- `npm run dev` — serveur de développement (proxy `/api` et `/health` vers `VITE_PROXY_TARGET`, défaut `http://localhost:8010`).
- `npm run build` — vérification TypeScript puis bundle.
- `npm run lint`, `npm run format:check`, `npm run test`.

Dans Docker Compose, le service `frontend` exécute le stage `development` avec `src/` monté en volume.
