# Home Ops

A self-hosted household management system: chores, a shared calendar, recipes and
meal planning, groceries, contacts, notes and health records — one private
application on hardware you own.

It is built for a household rather than a company, which mostly shows up in one
place: **members have different levels of trust.** A teenager, a housemate and a
grandparent should not see the same things, and "the admin can see everything" is
the wrong answer for a family's medical notes. So every record carries its own
visibility, and there is no administrator override on any of it.

Runs as **one Docker container** — application, web server and database together —
with a single volume and a single port.

## What it does

| Module | What it covers |
|---|---|
| **Tasks** | Assignments, categories, priorities, subtasks, recurring chores |
| **Notes** | A shared noticeboard with tags, pinning and a shared order |
| **Calendar** | Month and agenda views, RFC 5545 recurrence, three edit scopes, drag to move |
| **Kitchen** | Recipes with structured ingredients, import from a web page or a Mealie backup, meal planning |
| **Shopping** | Many lists, per-list visibility, transfers between them, generated from the meal plan |
| **Contacts** | A household directory with vCard import and export |
| **Health** | Vitals, medications, lab results and activity, shared per person |

Two ideas run through all of it. **Every member owns a colour** that follows them
into every list, so you find your own row before you read a word. And
**visibility is per record** — private, shared with named people, or the whole
household — enforced on every read path.

## Requirements

- Docker, with Compose v2 (`docker compose`, not `docker-compose`)
- About 1 GB of disk for the image, plus whatever your data comes to
- No cloud account, no API key, no external service

## Installation

**1. Set a database password.** Do this *before* the first start — Postgres bakes
it into the data directory on first boot, and changing it afterwards does not
change the user.

```sh
cp .env.example .env
```

Then edit `.env` and set:

```ini
POSTGRES_PASSWORD=<something long and random>
RUN_MIGRATIONS=true
APP_HOSTS=localhost,192.168.1.50        # your server's LAN address
```

`RUN_MIGRATIONS=true` matters: it creates the database schema. Left off, the app
starts but has no tables, and the first page fails.

**2. Start it.**

```sh
docker compose up -d --build
```

The first build takes a few minutes. If your Docker's Compose has trouble
building, build the image directly and then start it:

```sh
docker build -t home-ops:single .
docker compose up -d
```

**3. Open it** at <http://localhost:8080> — or the address in `APP_HOSTS`, on the
port in `WEB_PORT`.

The first visit shows a setup page, because no account exists yet. Whoever
completes it becomes the administrator. That page stops existing once there is a
user.

## Configuration

`.env` is written so each value appears once. Four lines cover a normal install:

| | |
|---|---|
| `POSTGRES_PASSWORD` | Set before the first start. Any characters; it is escaped wherever it is used. |
| `WEB_PORT` | The port the app is served on. Written here and nowhere else. |
| `APP_HOSTS` | Every hostname and LAN address you will reach it on — names only, comma separated. Each becomes an allowed origin. |
| `PUBLIC_BASE_URL` | Your HTTPS address, if you put it behind a tunnel or a reverse proxy. |

`DATABASE_URL` and `TRUSTED_ORIGINS` are assembled from those and should stay
blank unless you need something they cannot express.

### Reaching it over HTTPS

The app is designed to answer on several addresses at once — a public HTTPS
hostname, a VPN name, and a LAN address — because a household reaches it from
different places. Two consequences:

- `SESSION_COOKIE_SECURE` defaults to **false**, because a `Secure` cookie is
  silently dropped over plain HTTP and would lock out the LAN. Set it true only
  if *every* path is HTTPS.
- TLS is not handled here. Put a tunnel or a reverse proxy in front and add the
  hostname to `PUBLIC_BASE_URL`.

## Everyday operation

**Update to a newer version.** Your data lives in a Docker volume, not the image,
so replacing the image keeps it:

```sh
docker compose down
docker compose up -d --build
docker compose exec app home-ops-migrate      # if the schema changed
```

**Apply migrations by hand** (what `RUN_MIGRATIONS=true` does for you on start):

```sh
docker compose exec app home-ops-migrate
docker compose exec app home-ops-migrate current
```

**Back it up.** Everything is under `/data` in the one volume — the database and
the uploaded recipe images together:

```sh
docker run --rm -v home-ops_home_ops_data:/data -v "$PWD:/out" \
  alpine tar czf /out/home-ops-backup.tar.gz -C /data .
```

A database dump on its own is *not* a complete backup: recipe images live beside
it. Keep the archive somewhere other than the machine it came from.

**Logs.**

```sh
docker compose logs -f app
```

## Troubleshooting

**The first page errors, or nothing loads.** The schema was never created. Set
`RUN_MIGRATIONS=true` in `.env` and restart, or run
`docker compose exec app home-ops-migrate`.

**"Password authentication failed" in the logs.** `POSTGRES_PASSWORD` was changed
after the first start. Postgres fixed it at initialisation. Either put the
original back, or start over with a fresh volume
(`docker compose down -v` — **this deletes your data**).

**Locked out of the app.** Five failed sign-ins for one username in fifteen
minutes locks that name out; it clears itself after the window. The message for
that is *"Too many failed attempts"*, which is different from a wrong password.

**Port already in use.** Change `WEB_PORT` in `.env`.

**Nothing at your LAN address, but localhost works.** Add that address to
`APP_HOSTS` and restart. Origins are explicit on purpose — the app never trusts
the `Host` header to work out what it is called.

## How it is built

Python 3.12 + FastAPI · PostgreSQL 16 · React 19 + TypeScript + Vite ·
SQLAlchemy 2 + Alembic · nginx, supervised by s6-overlay. No cloud services at
runtime, no telemetry, no Redis.

One container holds all three processes with one volume at `/data`. nginx serves
the built frontend and proxies `/api` to the application over loopback, so the
browser only ever sees one origin and every URL in the app is relative.

Notable inside:

- **Recurrence** is `dateutil.rrule`, never hand-rolled. Events store a start
  instant *and* an IANA timezone name, and expansion happens in local wall-clock
  time — otherwise a 6pm swimming lesson drifts an hour for half the year.
- **Permissions** are two separate questions: may you use this feature, and may
  you see this row. Administrators bypass the first and nothing bypasses the
  second.
- **Uploads are re-encoded, never stored as they arrive**, which discards EXIF —
  routinely the GPS coordinates of somebody's kitchen — and defuses a file
  pretending to be an image.
- **Importing a recipe from a URL** is the one place the server connects to an
  address a user chose, so it validates every resolved IP, pins the connection to
  it, and re-checks every redirect. On a home network that code is one mistake
  away from reading the router's admin page.

## Licence

MIT — see [LICENSE](LICENSE).
