# Chai & Trails

A discussion forum for road trips, treks and slow journeys across India. The
application provides a category and forum hierarchy, threaded discussions with
Markdown posts and inline photographs, member profiles with uploaded profile
pictures, and full-text search over thread titles and post bodies.

The project is built with Django and PostgreSQL. Developed as part of my curriculum 
in B.Sc. Computer Science under the University of Mumbai.

---

## 1. Technology stack

| Layer | Technology | Version |
| --- | --- | --- |
| Language | Python | 3.14 or later |
| Web framework | Django | 6.1 |
| Database | PostgreSQL (in Docker) | 18 |
| Database driver | psycopg, with connection pooling | 3.3 |
| Password hashing | Argon2id, via argon2-cffi | 25.1 |
| Post rendering | markdown-it-py, sanitised with nh3 | 4.2 / 0.3 |
| Image processing | Pillow | 12.3 |
| Rich-text editor | TipTap (ProseMirror), bundled with esbuild | 3.31 |
| Package manager | uv | — |

Search is performed by PostgreSQL itself, using generated `tsvector` columns and
GIN indexes rather than an external search service.

---

## 2. Prerequisites

- **uv** — the Python package and project manager
  (<https://docs.astral.sh/uv/getting-started/installation/>).
- **Docker**, with Compose, which runs the PostgreSQL server.

Node.js need not be installed. The single JavaScript build step is executed
through `uvx`, which fetches a temporary Node distribution on demand.

---

## 3. Installation

### 3.1 Configuration file

The project reads its settings from a `.env` file, which is consumed both by
Django and by Docker Compose. A template is provided:

```sh
cp env.example .env
```

A Django secret key is generated with the following command, and the result is
placed in the `DJANGO_SECRET_KEY` field of `.env`:

```sh
uv run python -c "from django.core.management.utils import get_random_secret_key as k; print(k())"
```

A password of the developer's choosing is then entered for `DB_PASSWORD`. The
remaining fields may be left at their template values.

### 3.2 Database container

```sh
docker compose up -d --wait
```

This starts PostgreSQL 18 using the credentials from `.env` and stores its data
in a Docker named volume, `chai_and_trails_pgdata`. The `--wait` flag causes the
command to return only once the server is accepting connections.

### 3.3 Schema and initial data

```sh
uv run python manage.py migrate
uv run python manage.py import_forums
```

The first command creates the database schema. The second populates the board
structure — categories, forums, subforums and a set of sample threads — from
`forum/data/forums.json`.

No separate installation or compilation step is required for the Python code.
`uv run` reads `pyproject.toml` and `uv.lock`, creates the virtual environment in
`.venv/` if it is absent, and installs the locked dependencies before executing
the command. The first invocation therefore takes noticeably longer than
subsequent ones.

### 3.4 Administrative account

The imported sample members exist only to populate the discussion threads. Each
is created with an unusable password hash, so none of them can sign in, and none
holds staff privileges. An administrative account must therefore be created
explicitly:

```sh
uv run python manage.py createsuperuser
```

The command prompts for a username, an email address and a password. The password
is validated against Django's configured validators and stored as an Argon2id
hash; it is never written to disk in plain form.

### 3.5 Running the application

```sh
uv run python manage.py runserver
```

The forum is then served at <http://127.0.0.1:8000/> and the administrative
interface at <http://127.0.0.1:8000/admin/>.

---

## 4. Configuration reference

The `.env` file is excluded from version control, as it holds live credentials.
`env.example` documents the same fields with the secrets left blank.

| Variable | Purpose |
| --- | --- |
| `DJANGO_SECRET_KEY` | Signs session cookies and password-reset tokens. Each installation must generate its own. |
| `DB_NAME` | Database name. |
| `DB_USER` | Database role name. |
| `DB_PASSWORD` | Password for that role. |
| `DB_HOST` | `localhost` for the Compose configuration. |
| `DB_PORT` | Port published on the host; `5432` by default. |

`DB_NAME`, `DB_USER` and `DB_PASSWORD` are applied by the PostgreSQL image only
when the data volume is first created. Altering them afterwards has no effect on
an existing database; section 10.3 describes how to start afresh.

---

## 5. Management commands

The commands used routinely during development are Django's own:

| Command | Purpose |
| --- | --- |
| `uv run python manage.py runserver` | Runs the development server. |
| `uv run python manage.py makemigrations` | Generates migrations after `forum/models.py` is changed. |
| `uv run python manage.py migrate` | Applies outstanding migrations. |
| `uv run python manage.py shell` | Opens an interactive shell with the project loaded. |

Besides these, the project defines two management commands of its own.

```sh
uv run python manage.py import_forums [path]
```

Loads the board structure and sample discussions from `forum/data/forums.json`,
or from the file given as `path`. The command is idempotent: it updates existing
records instead of duplicating them, and recomputes each forum's thread and post
counts on completion.

```sh
uv run python manage.py rerender_posts
```

Regenerates the stored HTML of every post from its original Markdown. Each post
retains both the Markdown its author wrote and the HTML that pages display, so
this command is required after any change to the rendering or sanitisation rules
in `forum/rendering.py`.

---

## 6. Testing

```sh
uv run python manage.py test forum
```

The server-side suite currently contains 223 tests, covering the models and their
constraints, posting and the counters it maintains, rendering and sanitisation,
image processing, authentication, the views and their query counts, search, and
the templates. Django creates a separate test database on the same server and
drops it afterwards, so the development data is unaffected; the container must
nevertheless be running. Tests that write files use a temporary directory rather
than `media/`.

```sh
cd frontend/editor && uvx --from nodejs-wheel npm test
```

The editor suite contains 19 tests, executed under jsdom.

---

## 7. The post editor

Threads and replies are composed in a visual editor built on TipTap, which is in
turn built on ProseMirror. The editor writes **Markdown** into the form's own
text box, so the server receives and stores the same representation whether or
not JavaScript is available; without it, the plain text box is used directly.

The editor source resides in `frontend/editor/src/` and is bundled by esbuild
into a single file, `static/js/editor.js`, which Django serves as an ordinary
static asset. That bundle is committed to the repository, so the application runs
without any build step. Rebuilding is necessary only when the editor itself is
modified:

```sh
cd frontend/editor
uvx --from nodejs-wheel npm install      # once, and after package.json changes
uvx --from nodejs-wheel npm run build    # writes static/js/editor.js
```

`frontend/editor/README.md` documents the editor's internal structure in detail.

---

## 8. Third-party software and licences

All third-party components used by this project are distributed under permissive
open-source licences, with the single exception noted in section 8.6.

### 8.1 Python dependencies

Declared in `pyproject.toml` and version-locked in `uv.lock`.

| Package | Version | Licence | Used for |
| --- | --- | --- | --- |
| Django | 6.1.1 | BSD-3-Clause | Web framework, ORM, authentication, administration |
| psycopg (`binary`, `pool`) | 3.3.5 | LGPL-3.0-only | PostgreSQL driver and connection pool |
| Pillow | 12.3.0 | MIT-CMU (HPND) | Decoding, re-encoding and resizing uploaded images |
| markdown-it-py | 4.2.0 | MIT | Rendering post Markdown to HTML |
| mdit-py-plugins | 0.6.1 | MIT | Markdown attribute syntax, used for image widths |
| nh3 | 0.3.7 | MIT | HTML sanitisation of rendered posts |
| anyascii | 0.3.3 | ISC | Transliterating non-Latin thread titles into URL slugs |
| python-dotenv | 1.2.3 | BSD-3-Clause | Reading the `.env` configuration file |

Transitive dependencies installed alongside them:

| Package | Version | Licence |
| --- | --- | --- |
| argon2-cffi | 25.1.0 | MIT |
| argon2-cffi-bindings | 26.1.0 | MIT |
| asgiref | 3.12.1 | BSD-3-Clause |
| cffi | 2.1.1 | MIT-0 |
| mdurl | 0.1.2 | MIT |
| pycparser | 3.0 | BSD-3-Clause |
| sqlparse | 0.6.0 | BSD-3-Clause |

### 8.2 JavaScript bundled into the application

Forty-three packages are compiled into `static/js/editor.js`. Forty-two are
licensed under the MIT Licence and one, `entities` 4.5.0, under BSD-2-Clause. The
principal ones are:

| Package | Version | Licence | Used for |
| --- | --- | --- | --- |
| `@tiptap/core`, `@tiptap/starter-kit` and extensions | 3.31.3 | MIT | The visual editor framework |
| `@tiptap/pm` and the `prosemirror-*` packages | various | MIT | The underlying document model, view and commands |
| `prosemirror-markdown` | 1.13.7 | MIT | Serialising the document back to Markdown |
| `prosemirror-tables` | 1.8.5 | MIT | Table editing |
| `markdown-it`, `linkify-it`, `mdurl`, `uc.micro` | various | MIT | Markdown parsing within the editor |

Because these licences require their notices to be preserved in redistributed
builds, the build script `frontend/editor/scripts/licenses.js` extracts the full
licence text of every bundled package and writes it to
**`static/js/editor.js.LICENSES.txt`**, which is committed beside the bundle. The
bundle itself carries a banner comment pointing to that file. Both are regenerated
automatically by `npm run build`.

### 8.3 Typefaces

Two typefaces are loaded from Google Fonts: **Karla** for body text and
**Zilla Slab** for headings. Both are published under the SIL Open Font License
1.1, which permits use and redistribution in this context.

### 8.4 Platform software

| Component | Licence |
| --- | --- |
| PostgreSQL 18 | PostgreSQL Licence (permissive, BSD-like) |
| Python 3.14 | Python Software Foundation Licence |
| Docker Engine | Apache-2.0 |
| uv | Apache-2.0 or MIT, at the user's option |

### 8.5 Development tools

These are used to build and test the project but form no part of the deployed
application.

| Tool | Version | Licence | Purpose |
| --- | --- | --- | --- |
| esbuild | 0.28.2 | MIT | Bundling the editor into `static/js/editor.js` |
| jsdom | 30.0.1 | MIT | A DOM implementation for the editor's tests |
| nodejs-wheel (via `uvx`) | 11.17 (npm) | Node.js is MIT | Supplies Node and npm without a system installation |

The integrated development environment used for the project is **JetBrains
PyCharm**, as indicated by the `.idea/` directory in the repository. PyCharm
Community Edition is distributed under Apache-2.0; the Professional Edition is
commercial software, available to students under JetBrains' free educational
licence. The IDE's configuration directory is incidental to the project and the
application does not depend on it.

### 8.6 A note on licence compatibility

Every component listed above is permissively licensed — MIT, BSD, ISC, Apache-2.0,
HPND or the SIL Open Font License — with one exception. **psycopg**, the
PostgreSQL driver, is distributed under the **LGPL-3.0-only** licence, which is a
weak copyleft licence. The project uses psycopg as an unmodified library imported
at runtime, which is precisely the use the LGPL is designed to permit; the
obligations it imposes concern redistributing a *modified* psycopg, which this
project does not do. No component is licensed under the GPL proper, so the
project as a whole may be licensed at its authors' discretion.

---

## 9. Repository layout

```
chai_and_trails/        Project settings and the root URL configuration
forum/                  The application
  models.py               User, Category, Forum, Thread, Post, Attachment
  views.py                Views, both class-based and function-based
  forms.py                Registration, posting and profile forms
  admin.py                Administrative interface configuration
  photos.py               Validation and cleaning of uploaded images
  posting.py              Thread and reply creation, and the counters it maintains
  rendering.py            Markdown to HTML, followed by sanitisation
  templatetags/           Template filters
  management/commands/    import_forums, rerender_posts
  migrations/             Database migrations, 0001 to 0011
  data/forums.json        The board structure read by import_forums
  tests.py                The server-side test suite
templates/              Page templates; partials/ holds the shared fragments
static/                 css/, js/, img/ — served by Django during development
frontend/editor/        Source of static/js/editor.js
media/                  Uploaded photographs (excluded from version control)
compose.yaml            The PostgreSQL service definition
```

---

## 10. Database administration

### 10.1 Inspecting the database

```sh
docker compose exec db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
```

A software like DBeaver can also be connected to Postgres.

### 10.2 Backup and transfer

The data resides in the Docker volume rather than in the image, so `docker save`
and `docker commit` do **not** capture it; either would produce an apparently
correct container that starts up empty. A logical dump is used instead, and is
also far smaller.

```sh
# Create a dump
docker compose exec -T db sh -c \
  'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc --no-owner --no-privileges' > chai.dump

# Restore a dump
docker compose exec -T db sh -c \
  'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner --clean --if-exists' < chai.dump
```

`--no-owner` allows the dump to be restored under whatever role the receiving
installation uses. Uploaded photographs are stored as files rather than database
rows, so `media/` must be transferred separately.

A dump contains every member's email address and password hash. It is therefore
appropriate for a backup or a trusted collaborator, but should not be attached to
a public issue or shared link. An installation that merely needs a working forum
requires no dump at all: `migrate` followed by `import_forums` reproduces the
entire board without any personal data.

### 10.3 Stopping the database, and resetting it

```sh
docker compose stop     # Container retained, data retained
docker compose down     # Container removed, data retained
docker compose down -v  # Removes the volume and all data within it
```

`docker compose down` is safe, as named volumes survive it. The `-v` flag is the
one option that destroys the data; `docker volume prune -a` and
`docker system prune --volumes` will likewise remove the volume once no container
refers to it.

A deliberate reset is occasionally required — in particular, changing `DB_USER` or
`DB_PASSWORD` takes effect only on a newly created volume:

```sh
docker compose down -v && docker compose up -d --wait
uv run python manage.py migrate && uv run python manage.py import_forums
```

---

## 11. Limitations of the present configuration

The settings in `chai_and_trails/settings.py` are those of a development
installation. Before the application could be exposed publicly, at minimum:

- `DEBUG = True` and `ALLOWED_HOSTS = []` are presently hard-coded and would have
  to be supplied from the environment, with debugging disabled.
- `DEBUG` also governs how uploaded photographs are served
  (`chai_and_trails/urls.py`). In deployment, `STORAGES['default']` would point at
  object storage, and `media/` would be served from there rather than by Django.
- Static files would be collected with `collectstatic` and served by the web
  server or a content delivery network.
- `compose.yaml` publishes PostgreSQL on all interfaces (`0.0.0.0:5432`), making
  the server reachable by any host on the same network. Binding it to
  `127.0.0.1:${DB_PORT:-5432}:5432` restricts it to the local machine, which is
  sufficient for this configuration.
- Django's own deployment checklist should be reviewed:
  `uv run python manage.py check --deploy`.

Features described in the project plan but not yet implemented are per-member
unread markers and the deployment configuration itself.

---

## 12. Future plans

The principal objective is to make the forum publicly available. Several features
remain to be implemented before that would be worthwhile, among them a points
system recognising members' contributions and a set of moderation tools. Public
hosting also carries a recurring cost. However, I plan to undertake it as soon as
it becomes feasible.
