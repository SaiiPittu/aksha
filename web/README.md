# Aksha web app

The browser interface to an Aksha server: chat with agents, answer their questions and
approvals inline, open files and artifacts in a view panel, and manage the organisation
(members, invites, API tokens, audit log).

Vite, React 19, TypeScript, Tailwind CSS v4 and shadcn/ui. Routing with React Router
(library mode), server state with TanStack Query, response checks with Zod.

## Run it

```sh
aksha serve --port 8787          # the API server, in another terminal
cd web
nice -n 19 npm install
npm run dev                      # http://localhost:5173, proxies /api to 127.0.0.1:8787
```

Set `AKSHA_API` to proxy somewhere else (`AKSHA_API=http://127.0.0.1:9000 npm run dev`).

| Script | What |
|---|---|
| `npm run dev` | dev server with the `/api` proxy |
| `npm run build` | type check (`tsc -b`) and a production build in `web/dist/` |
| `npm run lint` | ESLint (flat config, `eslint.config.js`) |
| `npm test` | Vitest with jsdom and Testing Library (`npm test -- --run` for one pass) |
| `npm run e2e` | Playwright against a real `aksha serve` it starts itself (fake mode, a temporary `AKSHA_HOME`, the built app via `--web`); `E2E_SHOTS=DIR` also saves screenshots |

## The look lives in one file

**`src/theme.css` is the one file that sets the look**: every colour, radius, font and the
panel width, for light and dark. `src/index.css` maps those tokens into Tailwind once.
Components use tokens only (`bg-primary`, `text-status-wait`, `border-status-bad/30`),
never raw colours, so replacing `theme.css` re-skins the whole app, including the chat
components. The status vocabulary (wait, run, ok, bad, idle) is shared with the terminal UI.

## Layout

```
src/
  api/                 the ONLY place URLs and wire shapes live
    client.ts          request(), ApiError, CSRF header, Command (command IDs, retries)
    schemas.ts         Zod schemas of the responses the app reads
    endpoints.ts       every route: auth, org, tokens, runtime, session commands
    queries.ts         TanStack Query keys and hooks
  features/
    auth/              guard decisions (pure), viewer and role rules, form checks
    session/           the session view model, the live store and the useSession seam (see below)
  app/
    auth/              sign in, sign up, invite, try, gates, claim and save dialogs
    chat/              chat screen, parts, interrupt cards, composer, chat route
    panel/ views/      the view panel and its renderers by view kind
    settings/          profile, members, invites, tokens, audit, runtime
    pages/             inbox, sessions, files (over the reads API), evals
    shell/             app shell, banners, status chips, small shared pieces
  components/ui/       shadcn/ui primitives
  components/ai-elements/  chat components copied in as our own code
  router.tsx           every route
  theme.css            the look
e2e/                   Playwright: global setup (build, start the server), fixtures, the end-to-end spec
```

## Routes

`/sign-in`, `/sign-up` (first user, creates the org), `/invite/:token`, `/try` (guest
sandbox, when the server allows guests), `/` (new chat), `/s/:session` (chat; the open
view is `?view=<view id>`), `/inbox`, `/sessions`, `/files`, `/evals`,
`/settings/{profile,members,invites,tokens,audit,runtime}`.

The guard reads `GET /api/v1/auth/status` and `GET /api/v1/auth/me`. In local mode (a
fresh server on your own machine) the app opens straight away with a "claim this
workspace" banner; signed out, it goes to sign in and comes back to where you were.

## The session seam

The chat screen renders a `SessionHandle` and nothing else:

```ts
// src/features/session/useSession.ts
function useSession(sessionId: string | null): SessionHandle
interface SessionHandle { state: SessionState; commands: SessionCommands }
```

`SessionState` (in `src/features/session/types.ts`) holds messages with parts, runs,
interrupts by ID, artifacts, the stream state and the cursor. The live store behind it:

| File | What |
|---|---|
| `client.ts` | `SessionClient`: one stream per open session on `GET /sessions/{s}/events?after=<cursor>`; tracks the cursor, reconnects with backoff from it, treats 30 s without an event or heartbeat as dropped, passes `synchronized` and `resync` through. The default connection reads SSE with `fetch` (credentials included), because a browser `EventSource` cannot see heartbeat comments or event types it has no listener for. |
| `reducer.ts` | `sessionReducer(store, message)`: pure, deduplicated by seq, the same fold for replay and live. Steps group per Task, artifacts count their versions per slot, interrupts and approvals update in place. |
| `commands.ts` | send (a new chat names its session `S-<12 hex>`), steer by lane, answer, approve with the full signature, reject with a note, stop, review accept or reject, budget grant or finish, and uploads with per-file progress. |
| `wire.ts` | the stream message types and the event types the reducer knows. |

Tests fold the recorded streams in `src/features/session/__fixtures__/` (the bundled
fictional sample world, tool output trimmed).

## API shapes

Response schemas follow the accounts API: they accept the documented fields and tolerate
extra ones, and list endpoints accept a bare array or an object holding it. Errors are the
server's `{error: {code, message, details}}`, shown inline with the code.
