# Band Office

A web app for running a First Nations band office. It covers members, staff, housing, programs (including ISC funding), council resolutions and by-laws, finances, infrastructure, tasks, documents, leave requests, purchase requisitions, incident reports and meeting requests.

It is a single Flask app with an SQLite database and a plain HTML/JavaScript frontend. Everyone signs in, and what each person can see depends on their role.

## Quick start (development)

```bash
pip install -r requirements.txt
python3 app.py                     # http://localhost:5000
```

On first start the app creates `data/band_office.db` and loads sample data. There are no accounts yet, so create the first admin:

```bash
flask --app app create-user alice                     # prompts for a password
flask --app app create-user bob --role finance --password 'at-least-12-chars'
```

Passwords must be at least 12 characters. After that, admins can manage users in the app under **Users**.

If port 5000 is taken, use another one: `BAND_OFFICE_PORT=5050 python3 app.py`.

## Running with Docker

```bash
docker compose up -d --build
docker compose exec band-office flask --app app create-user alice
```

- **Port:** the app is published on `127.0.0.1:5000` only. Put a reverse proxy with HTTPS in front of it to reach it from other machines (see [Behind a reverse proxy](#behind-a-reverse-proxy)).
- **Data:** the database, the generated secret key and uploaded documents live in the `band-office-data` and `band-office-documents` volumes.
- **User:** the container runs as an unprivileged user (uid 10001).

**Upgrading from an image that ran as root:** existing volumes contain files owned by root, so the container stops with an error at startup. Fix ownership once, then start it again:

```bash
docker compose run --rm --user 0 --entrypoint chown band-office -R 10001:10001 /app/data /app/documents
```

## Roles

| Role | Can open |
|---|---|
| `admin` | Everything, including **Users** and the **Audit Log** |
| `finance` | Finances, budget reports, finance CSV export, and the general sections |
| `registry` | Members, member statistics, member CSV export, and the general sections |
| `staff` | The general sections only |

The general sections are dashboard, notifications, housing, programs, council, infrastructure, tasks, documents, and leave, purchase, incident and meeting requests.

Staff records include salaries, so they are admin-only. The server enforces every check. The page only hides what the server says a role can't open, and dashboard widgets follow the same rules.

## Configuration

All settings are environment variables.

| Variable | Default | Purpose |
|---|---|---|
| `BAND_OFFICE_HOST` | `127.0.0.1` | Address to listen on (the Docker image sets `0.0.0.0`) |
| `BAND_OFFICE_PORT` | `5000` | Port to listen on |
| `BAND_OFFICE_DATA_DIR` | `./data` | Where the database and generated secret key are stored |
| `BAND_OFFICE_SECRET_KEY` | generated | Signs session cookies. If unset, a key is generated and saved as `secret_key` in the data directory, so all workers share it and sign-ins survive restarts. |
| `BAND_OFFICE_COOKIE_SECURE` | `1` | Send the session cookie over HTTPS only. Browsers allow this on `http://localhost`. Set `0` only if people must use plain http from other machines. |
| `BAND_OFFICE_PROXY_COUNT` | `0` | Number of reverse proxies in front of the app. See below. |
| `BAND_OFFICE_CORS_ORIGIN` | empty | Origin allowed to call the API cross-origin. Empty means same-origin only. Sign-in cookies are same-site, so a different site can't sign in anyway. |
| `BAND_OFFICE_DEBUG` | `0` | Flask debug mode (development server only) |
| `BAND_OFFICE_WORKERS` | CPU × 2 + 1 | Number of gunicorn workers |
| `BAND_OFFICE_TIMEOUT` | `30` | gunicorn request timeout in seconds |
| `BAND_OFFICE_KEEPALIVE` | `5` | gunicorn keep-alive in seconds |
| `BAND_OFFICE_LOG_LEVEL` | `info` | gunicorn log level |
| `BAND_OFFICE_ACCESS_LOG` / `BAND_OFFICE_ERROR_LOG` | `-` (stdout) | gunicorn log destinations |

### Behind a reverse proxy

Set `BAND_OFFICE_PROXY_COUNT=1` (or the number of proxies) when the app sits behind nginx, Caddy or similar. The app then reads the client's real IP address and HTTPS status from the proxy's `X-Forwarded-For` and `X-Forwarded-Proto` headers.

- **Why it's needed:** sign-in limits are counted per IP address. Without this setting every user appears to come from the proxy's address, so one person's failed attempts could pause sign-in for everyone. The app logs a warning when it sees proxied requests without the setting.
- **When not to set it:** if there is no proxy. A client could then fake its address with an `X-Forwarded-For` header.

## Security model

- **Sessions:** signed cookies that last 8 hours, can't be read by page scripts, and are `SameSite=Lax`. Every write, including sign-in, needs a CSRF token tied to the session. Before signing in, the page gets one from `GET /api/auth/csrf`.
- **Ending sessions:** deactivating a user, changing their role or resetting their password signs them out everywhere. Users can't be deleted, only deactivated, so the audit history keeps their name. The last active admin can't be deactivated or demoted.
- **Sign-in limits:** each IP gets 5 failed attempts per username, and 20 across all usernames, within 15 minutes. Someone else's failures can't lock you out from your own computer. A blocked attempt gets the same response as a wrong password, so responses don't reveal which accounts exist. Attempted usernames are stored only as hashes.
- **Audit log:** every create, update and delete is recorded on the server in the same transaction as the change, with the username, plus sign-ins, sign-outs, failures and lockouts. Timestamps are in UTC. Clients can't write audit entries.
- **Documents:**
  - Uploads are limited to pdf, doc, docx, txt, jpg, jpeg and png. Files get unique names and are only served from `documents/`.
  - Allowed types open in the browser; anything else downloads.
  - A document's file is deleted with the document. Uploads never attached to a document are removed after 24 hours.
- **Frontend:** all database values are HTML-escaped before rendering.

## API

All endpoints are under `/api/` and need a signed-in session, except `POST /api/auth/login` and `GET /api/auth/csrf`. Writes need the `X-CSRF-Token` header.

| Endpoint | Purpose |
|---|---|
| `GET/POST /api/<resource>` | List or create a record |
| `GET/PUT/DELETE /api/<resource>/<id>` | Read, update or delete a record |
| `GET /api/dashboard`, `/api/dashboard/pending-approvals` | Dashboard figures |
| `GET /api/reports/budget-vs-actual`, `/program-summary`, `/member-stats` | Reports |
| `GET /api/export/members.csv`, `/api/export/finances.csv` | CSV exports |
| `POST /api/documents/upload`, `GET /api/documents/<id>/file` | Document files |
| `GET /api/audit-log?table=<name>` | The 100 most recent audit entries (admin) |
| `/api/auth/login`, `/logout`, `/me`, `/password`, `/csrf` | Sessions |
| `GET/POST /api/users`, `GET/PUT /api/users/<id>` | User management (admin) |

The resources are `members`, `staff`, `housing`, `programs`, `council`, `finances`, `infrastructure`, `notifications`, `tasks`, `documents`, `leave-requests`, `purchase-requisitions`, `incident-reports` and `meeting-requests`.

Behaviour of the generated endpoints:

- **Filtering:** lists filter on one field per resource (for example `?status=pending` or `?document_type=report`).
- **Search and paging:** the members and staff lists support `?search=`. Members also pages with `?page=` and `?per_page=` (up to 500).
- **Blank fields:** on create, a blank field gets the column's default. On update, a blank optional field is cleared; blank required fields are rejected.

## Tests

```bash
pip install pytest
python3 -m pytest -q
```

Tests use temporary databases and data directories and never touch `data/`.

## Project layout

| Path | Contents |
|---|---|
| `app.py` | The whole backend: schema, sample data, routes, roles, sign-in and the `create-user` command |
| `templates/index.html` | The frontend (a single page) |
| `wsgi_entry.py`, `gunicorn_config.py` | Production entry point and gunicorn settings |
| `Dockerfile`, `docker-compose.yml`, `entrypoint.sh` | Container build and startup |
| `tests/test_api.py` | API test suite |
| `data/`, `documents/` | Runtime data (not tracked by git) |
