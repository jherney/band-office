#!/bin/sh
set -e

# Initialize database (creates tables + seeds data if empty)
python3 -c "from app import init_db; init_db()"

# Start gunicorn
exec gunicorn -c gunicorn_config.py wsgi_entry:app
