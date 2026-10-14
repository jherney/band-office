#!/bin/sh
set -e

# Volumes created by older images (which ran as root) hold root-owned files
for dir in /app/data /app/documents; do
    if [ -n "$(find "$dir" ! -user "$(id -u)" -print -quit)" ]; then
        echo "error: $dir contains files not owned by uid $(id -u). Fix once with:" >&2
        echo "  docker compose run --rm --user 0 --entrypoint chown band-office -R 10001:10001 /app/data /app/documents" >&2
        exit 1
    fi
done

# Initialize database (creates tables + seeds data if empty)
python3 -c "from app import init_db; init_db()"

# Start gunicorn
exec gunicorn -c gunicorn_config.py wsgi_entry:app
