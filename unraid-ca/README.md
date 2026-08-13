# Home Ops — Unraid Community Applications templates

Community Applications templates for [Home Ops](https://github.com/g0browns/Home-ops),
a self-hosted household management system: chores, a shared calendar, recipes and
meal planning, groceries, contacts, notes and health records — one private
application on hardware you own.

Everything runs in **one container**: FastAPI, nginx and PostgreSQL 16 together
under s6-overlay, with a single appdata path and a single port.

| | |
|---|---|
| Template | [`templates/home-ops.xml`](templates/home-ops.xml) |
| Image | `ghcr.io/g0browns/home-ops:latest` |
| Container port | `8080` |
| Container data path | `/data` |
| Licence | MIT |

## Repository layout

Laid out to the
[Unraid Community Apps starter](https://github.com/unraid/unraid-community-apps-starter)
conventions:

```
ca_profile.xml        repository-level metadata shown in Apps
icon.svg              repository and application icon
LICENSE               MIT
README.md             this file
templates/
  home-ops.xml        one XML per Docker application
```

There is no `plugins/` directory — this repository ships a Docker application,
not a plugin.

## Installing

Until this repository is accepted into Community Applications, install it by
hand:

1. In Unraid, **Docker → Add Container**.
2. Paste this into **Template**:
   `https://raw.githubusercontent.com/g0browns/home-ops-unraid/main/templates/home-ops.xml`
3. Fill in the four settings below and **Apply**.

## Settings that matter on first start

Four values decide whether the first page works. The rest have sensible
defaults.

**`POSTGRES_PASSWORD`** — set it *before* the first start. PostgreSQL bakes it
into the data directory on first boot and changing it afterwards does not
re-create the user. If you get it wrong, the fix is either to put the original
back or to delete the appdata directory and start again.

**`RUN_MIGRATIONS`** — leave it `true`. It creates the database schema. Left
off, the container starts happily and the first page fails.

**`APP_HOSTS`** — every hostname and IP you will open the app on over plain
HTTP, comma separated, **names only** (no scheme, no port):
`localhost,192.168.1.10,tower.local`. Each becomes an allowed browser origin.
The app never trusts the `Host` header to work out what it is called, so an
address that is not listed here can load the page but cannot sign in.

**`WEB_PORT`** — the **host** port you mapped, not the container port. It does
not bind anything; it is the port used to build those origins. If your address
bar says `:8090` and this says `8080`, sign-in fails on that address.

### Appdata

`/data` holds the PostgreSQL cluster (`/data/postgres`) and uploaded recipe
images (`/data/uploads`) together.

Point it at a **cache pool** — `/mnt/cache/appdata/home-ops` — rather than the
`/mnt/user` fuse path. PostgreSQL does not behave well on shfs, and a share that
can move to the array mid-write is not somewhere to keep a database.

Back up that one directory and you have a complete backup. A database dump on
its own is not: the recipe images live beside it.

### HTTPS

Set `PUBLIC_BASE_URL` to your external address (`https://home.example.com`) if
you front the app with a reverse proxy or a Cloudflare Tunnel. Leave
`SESSION_COOKIE_SECURE` at `false` unless *every* path is HTTPS — a `Secure`
cookie is silently dropped over plain HTTP, which would lock out your LAN
address.

### Using a PostgreSQL you already run

Set `DATABASE_URL` to
`postgresql+psycopg://user:password@host:5432/home_ops` and the bundled
PostgreSQL simply goes unused. Percent-encode any `@` in the password (`%40`).

## First run

Open `http://<your-server>:8080`. The first visit shows a setup page, because no
account exists yet — whoever completes it becomes the administrator, and that
page stops existing once there is a user.

## Everyday operation

**Update.** Your data is in the appdata path, not the image, so pulling a new
image keeps it. Migrations are applied on start while `RUN_MIGRATIONS` is
`true`; otherwise run `home-ops-migrate` from the container console.

**Locked out.** Five failed sign-ins for one username in fifteen minutes locks
that name out; it clears itself. *"Too many failed attempts"* is a different
message from a wrong password.

**Nothing at your LAN address, but `localhost` works.** Add that address to
`APP_HOSTS` and restart.


MIT — see [LICENSE](LICENSE). Home Ops itself is MIT too.
