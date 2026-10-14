from flask import Flask, jsonify, request, send_from_directory, send_file, session, g, has_app_context
from flask_cors import CORS
import click
import functools
import hashlib
import hmac
import sqlite3
import os
import csv
import io
import json
import logging
import re
import secrets
import time
import uuid
from datetime import timedelta
from contextlib import contextmanager
from werkzeug.exceptions import InternalServerError
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get('BAND_OFFICE_DATA_DIR') or os.path.join(BASE_DIR, 'data')
DB_PATH = os.path.join(DATA_DIR, 'band_office.db')
DOCUMENTS_DIR = os.path.realpath(os.path.join(BASE_DIR, 'documents'))
ALLOWED_DOCUMENT_EXTENSIONS = {'.pdf', '.doc', '.docx', '.txt', '.jpg', '.jpeg', '.png'}

app = Flask(__name__, static_folder='static', template_folder='templates')
logger = logging.getLogger(__name__)

# Number of reverse proxies in front of the app. Sign-in limits are per client IP,
# so behind a proxy this must be set or every user shares the proxy's address.
PROXY_COUNT = int(os.environ.get('BAND_OFFICE_PROXY_COUNT', '0'))
if PROXY_COUNT:
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=PROXY_COUNT, x_proto=PROXY_COUNT)

# Same-origin only unless an origin is configured explicitly
CORS_ORIGIN = os.environ.get('BAND_OFFICE_CORS_ORIGIN', '').strip()
if CORS_ORIGIN:
    CORS(app, origins=[CORS_ORIGIN])

HOST = os.environ.get('BAND_OFFICE_HOST', '127.0.0.1')
PORT = int(os.environ.get('BAND_OFFICE_PORT', '5000'))
DEBUG = os.environ.get('BAND_OFFICE_DEBUG', '0').lower() in ('1', 'true', 'yes')


def load_secret_key():
    key = os.environ.get('BAND_OFFICE_SECRET_KEY', '').strip()
    if key and key != 'change-this-in-production':
        return key
    # Persist a generated key in the data volume so every worker signs sessions
    # with the same key and logins survive restarts
    path = os.path.join(DATA_DIR, 'secret_key')
    if not os.path.exists(path):
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as f:
            f.write(secrets.token_hex(32))
        try:
            os.link(tmp, path)  # atomic; fails if another worker created it first
        except FileExistsError:
            pass
        finally:
            os.remove(tmp)
    with open(path) as f:
        return f.read().strip()


app.secret_key = load_secret_key()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    # Browsers accept Secure cookies on http://localhost; set 0 only for plain-http access from other hosts
    SESSION_COOKIE_SECURE=os.environ.get('BAND_OFFICE_COOKIE_SECURE', '1').lower() in ('1', 'true', 'yes'),
    PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
)

# admin can do everything; other roles reach a resource only when listed for it
ROLES = ('admin', 'finance', 'registry', 'staff')
ANY_ROLE = ROLES
MIN_PASSWORD_LENGTH = 12
MAX_FAILED_LOGINS = 5  # per IP + username: someone else's failures can't lock you out
MAX_FAILED_LOGINS_PER_IP = 20  # across usernames, against password spraying
LOCKOUT = timedelta(minutes=15)
INVALID_LOGIN = 'Invalid username or password. After repeated failures, sign-in from your computer is paused for 15 minutes.'
USERNAME_RE = re.compile(r'^[A-Za-z0-9._-]{3,50}$')
# Compared against when the username is unknown, so failures take the same time either way
DUMMY_PASSWORD_HASH = generate_password_hash(secrets.token_hex(16))
# Section name -> roles; CRUD resources are added by register_crud_routes. Served to the frontend.
SECTION_ROLES = {'dashboard': ANY_ROLE, 'reports': ('finance', 'registry'), 'audit': (), 'users': ()}
PUBLIC_ENDPOINTS = {'index', 'static', 'send_static', 'favicon', 'health', 'login', 'login_csrf_token'}
# Unknown usernames can't be locked out, so log their failures at most this often
UNKNOWN_USER_AUDIT_INTERVAL = timedelta(minutes=1)


@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
    finally:
        conn.close()


def row_to_dict(row):
    return dict(row)


def query_all(conn, sql, params=()):
    return [row_to_dict(r) for r in conn.execute(sql, params).fetchall()]


def query_one(conn, sql, params=()):
    row = conn.execute(sql, params).fetchone()
    return row_to_dict(row) if row else None


def get_json_body():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ValueError("Request body must be a JSON object")
    nested = [k for k, v in data.items() if isinstance(v, (dict, list))]
    if nested:
        raise ValueError(f"Fields must be scalar values: {', '.join(nested)}")
    return data


def get_positive_int_arg(name, default, maximum):
    try:
        value = int(request.args.get(name, default))
    except ValueError:
        raise ValueError(f"{name} must be an integer")
    if not 1 <= value <= maximum:
        raise ValueError(f"{name} must be between 1 and {maximum}")
    return value


def resolve_document_path(file_path):
    """Absolute path for a documents.file_path value, or None if it is not a file directly inside DOCUMENTS_DIR."""
    if not file_path:
        return None
    candidate = os.path.realpath(os.path.join(BASE_DIR, file_path))
    return candidate if os.path.dirname(candidate) == DOCUMENTS_DIR else None


def referenced_document_paths(conn):
    return {resolve_document_path(r[0]) for r in conn.execute("SELECT file_path FROM documents WHERE file_path IS NOT NULL")}


def remove_unreferenced_document_file(conn, file_path, referenced=None):
    path = resolve_document_path(file_path)
    if not path:
        return
    if path in (referenced if referenced is not None else referenced_document_paths(conn)):
        return
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except OSError as e:
        logger.warning(f"Could not remove document file {path}: {e}")


UPLOAD_NAME_RE = re.compile(r'^[0-9a-f]{32}_')
ORPHAN_UPLOAD_MAX_AGE = 24 * 3600


def sweep_orphan_uploads(conn):
    """Delete uploads never attached to a document. The age limit leaves time to attach a fresh upload."""
    cutoff = time.time() - ORPHAN_UPLOAD_MAX_AGE
    referenced = referenced_document_paths(conn)
    for name in os.listdir(DOCUMENTS_DIR):
        if not UPLOAD_NAME_RE.match(name):
            continue  # only files this app uploaded
        path = os.path.join(DOCUMENTS_DIR, name)
        try:
            if os.path.getmtime(path) < cutoff:
                remove_unreferenced_document_file(conn, os.path.join('documents', name), referenced)
        except FileNotFoundError:
            pass


def write_audit(conn, action, table_name, record_id, old_values=None, new_values=None, user_name=None):
    to_json = lambda v: json.dumps(v, default=str) if v is not None else None
    if user_name is None:
        user = g.get('user') if has_app_context() else None
        user_name = user['username'] if user else 'system'
    # created_at uses the column default (UTC, like every other timestamp)
    conn.execute(
        "INSERT INTO audit_log (action, table_name, record_id, old_values, new_values, user_name) VALUES (?, ?, ?, ?, ?, ?)",
        (action, table_name, record_id, to_json(old_values), to_json(new_values), user_name))


def has_role(user, roles):
    return user['role'] == 'admin' or user['role'] in roles


def require_roles(*roles):
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            if not has_role(g.user, roles):
                return jsonify({'error': 'You do not have access to this'}), 403
            return fn(*args, **kwargs)
        return wrapper
    return decorator


def is_blank(value):
    """Missing, null or whitespace-only. 0 and False are real values."""
    return value is None or (isinstance(value, str) and not value.strip())


def validate_required_fields(data, required_fields):
    missing = [f for f in required_fields if is_blank(data.get(f))]
    if missing:
        raise ValueError(f"Missing required fields: {', '.join(missing)}")


RESOURCE_REGISTRY = {
    'members': {
        'table': 'members',
        'roles': ('registry',),
        'fields': ['band_number', 'surname', 'given_name', 'middle_name', 'dob', 'gender',
                   'status_number', 'is_register', 'reserve', 'address', 'city', 'province',
                   'postal_code', 'phone', 'email', 'guardian', 'membership_date',
                   'transfer_from', 'transfer_date', 'notes'],
        'required': ['band_number', 'surname', 'given_name'],
        'search': ['surname', 'given_name', 'band_number', 'status_number'],
        'order_by': 'surname, given_name',
        'pagination': True,
    },
    'staff': {
        'table': 'staff',
        'roles': (),
        'fields': ['surname', 'given_name', 'position', 'department', 'start_date',
                   'end_date', 'status', 'phone', 'email', 'salary', 'benefits_eligible', 'notes'],
        'required': ['surname', 'given_name'],
        'search': ['surname', 'given_name', 'position', 'department'],
        'order_by': 'surname, given_name',
        'pagination': False,
    },
    'housing': {
        'table': 'housing',
        'fields': ['address', 'unit_type', 'bedrooms', 'bathrooms', 'occupants',
                   'member_id', 'status', 'condition', 'maintenance_notes',
                   'assigned_date', 'isc_funding_eligible'],
        'required': ['address'],
        'search': [],
        'order_by': 'address',
        'pagination': False,
        'filter_field': 'status',
        'join_member': True,
    },
    'programs': {
        'table': 'programs',
        'fields': ['name', 'program_type', 'department', 'funding_source', 'budget',
                   'spent', 'start_date', 'end_date', 'status', 'description',
                   'isc_program_code', 'reporting_requirements'],
        'required': ['name'],
        'search': [],
        'order_by': 'name',
        'pagination': False,
        'filter_field': 'status',
    },
    'council': {
        'table': 'council',
        'fields': ['item_type', 'title', 'date', 'attendees', 'resolution_text',
                   'motion', 'vote_for', 'vote_against', 'abstain', 'status',
                   'band_manager_review', 'isc_submitted', 'isc_reference', 'notes'],
        'required': ['item_type', 'title', 'date'],
        'search': [],
        'order_by': 'date DESC',
        'pagination': False,
        'filter_field': 'item_type',
    },
    'finances': {
        'table': 'finances',
        'roles': ('finance',),
        'has_updated_at': False,
        'fields': ['category', 'type', 'amount', 'date', 'description',
                   'funding_source', 'reference_number', 'isc_grant_id', 'approved_by'],
        'required': ['category', 'type', 'amount', 'date'],
        'search': [],
        'order_by': 'date DESC',
        'pagination': False,
        'filter_field': 'type',
    },
    'infrastructure': {
        'table': 'infrastructure',
        'fields': ['name', 'asset_type', 'location', 'condition', 'estimated_value',
                   'last_inspection', 'isc_funding_program', 'maintenance_schedule', 'notes'],
        'required': ['name'],
        'search': [],
        'order_by': 'name',
        'pagination': False,
    },
    'notifications': {
        'table': 'notifications',
        'has_updated_at': False,
        'fields': ['title', 'message', 'type', 'priority', 'related_table',
                   'related_id', 'is_read'],
        'required': ['title', 'message'],
        'search': [],
        'order_by': 'created_at DESC',
        'pagination': False,
    },
    'tasks': {
        'table': 'tasks',
        'fields': ['title', 'description', 'status', 'priority', 'assigned_to',
                   'due_date', 'related_table', 'related_id'],
        'required': ['title'],
        'search': [],
        'order_by': 'due_date ASC',
        'pagination': False,
        'filter_field': 'status',
    },
    'documents': {
        'table': 'documents',
        'fields': ['title', 'document_type', 'related_table', 'related_id',
                   'file_path', 'content', 'created_by'],
        'required': ['title'],
        'search': [],
        'order_by': 'created_at DESC',
        'pagination': False,
        'filter_field': 'document_type',
        'file_field': 'file_path',
    },
    'leave_requests': {
        'table': 'leave_requests',
        'has_updated_at': False,
        'fields': ['staff_id', 'leave_type', 'start_date', 'end_date', 'reason',
                   'status', 'approved_by', 'approved_at'],
        'required': ['staff_id', 'leave_type', 'start_date', 'end_date'],
        'search': [],
        'order_by': 'created_at DESC',
        'pagination': False,
        'filter_field': 'status',
        'join_staff': True,
    },
    'purchase_requisitions': {
        'table': 'purchase_requisitions',
        'fields': ['title', 'description', 'amount', 'vendor', 'requested_by',
                   'department', 'status', 'priority', 'approval_notes'],
        'required': ['title', 'amount'],
        'search': [],
        'order_by': 'created_at DESC',
        'pagination': False,
        'filter_field': 'status',
    },
    'incident_reports': {
        'table': 'incident_reports',
        'has_updated_at': False,
        'fields': ['incident_type', 'description', 'location', 'severity',
                   'reported_by', 'date_reported', 'date_occurred', 'status',
                   'follow_up_actions', 'resolved_at'],
        'required': ['incident_type', 'description', 'date_reported'],
        'search': [],
        'order_by': 'date_reported DESC',
        'pagination': False,
        'filter_field': 'status',
    },
    'meeting_requests': {
        'table': 'meeting_requests',
        'has_updated_at': False,
        'fields': ['title', 'description', 'requested_by', 'meeting_date',
                   'start_time', 'end_time', 'location', 'attendees', 'status',
                   'approved_by', 'approved_at'],
        'required': ['title', 'meeting_date'],
        'search': [],
        'order_by': 'meeting_date DESC',
        'pagination': False,
        'filter_field': 'status',
    },
}


def build_list_query(resource_name, search_term=None, filter_value=None, page=1, per_page=50):
    config = RESOURCE_REGISTRY[resource_name]
    table = config['table']
    query = f"SELECT * FROM {table} WHERE 1=1"
    params = []

    if search_term and config['search']:
        search_clause = " OR ".join([f"{f} LIKE ?" for f in config['search']])
        query += f" AND ({search_clause})"
        params.extend([f'%{search_term}%'] * len(config['search']))

    if filter_value and config.get('filter_field'):
        query += f" AND {config['filter_field']} = ?"
        params.append(filter_value)

    query += f" ORDER BY {config['order_by']}"

    if config['pagination']:
        query += " LIMIT ? OFFSET ?"
        params.extend([per_page, (page - 1) * per_page])

    return query, params


def build_count_query(resource_name, search_term=None, filter_value=None):
    config = RESOURCE_REGISTRY[resource_name]
    table = config['table']
    query = f"SELECT COUNT(*) FROM {table} WHERE 1=1"
    params = []

    if search_term and config['search']:
        search_clause = " OR ".join([f"{f} LIKE ?" for f in config['search']])
        query += f" AND ({search_clause})"
        params.extend([f'%{search_term}%'] * len(config['search']))

    if filter_value and config.get('filter_field'):
        query += f" AND {config['filter_field']} = ?"
        params.append(filter_value)

    return query, params


def register_crud_routes(app, resource_name, url_prefix):
    config = RESOURCE_REGISTRY[resource_name]
    table = config['table']
    safe_name = url_prefix.replace('-', '_')

    list_fn_name = f'_list_{safe_name}'
    create_fn_name = f'_create_{safe_name}'
    get_fn_name = f'_get_{safe_name}'
    update_fn_name = f'_update_{safe_name}'
    delete_fn_name = f'_delete_{safe_name}'

    def _list(item_id=None):
        try:
            with get_db() as conn:
                search = request.args.get('search', '')
                filter_field = config.get('filter_field')
                filter_value = request.args.get(filter_field, '') if filter_field else ''
                page, per_page = 1, 50
                if config['pagination']:
                    page = get_positive_int_arg('page', 1, 1_000_000)
                    per_page = get_positive_int_arg('per_page', 50, 500)

                query, params = build_list_query(resource_name, search or None, filter_value or None, page, per_page)
                rows = conn.execute(query, params).fetchall()

                count_query, count_params = build_count_query(resource_name, search or None, filter_value or None)
                total = conn.execute(count_query, count_params).fetchone()[0]

                result = [row_to_dict(r) for r in rows]

                if config.get('join_member'):
                    for item in result:
                        member = conn.execute("SELECT surname, given_name FROM members WHERE id = ?", (item.get('member_id'),)).fetchone()
                        item['member_surname'] = member['surname'] if member else ''
                        item['member_given_name'] = member['given_name'] if member else ''

                if config.get('join_staff'):
                    for item in result:
                        staff = conn.execute("SELECT surname, given_name FROM staff WHERE id = ?", (item.get('staff_id'),)).fetchone()
                        item['staff_surname'] = staff['surname'] if staff else ''
                        item['staff_given_name'] = staff['given_name'] if staff else ''

                if config['pagination']:
                    return jsonify({url_prefix: result, 'total': total, 'page': page, 'per_page': per_page})
                return jsonify(result)
        except ValueError as e:
            return jsonify({'error': str(e)}), 400
        except Exception as e:
            logger.error(f"Error listing {table}: {e}")
            return jsonify({'error': f'Failed to list {table}'}), 500

    def _create():
        try:
            data = get_json_body()
            validate_required_fields(data, config['required'])
            with get_db() as conn:
                # Leave blank fields out so the column default (or NULL) applies
                fields = [f for f in config['fields'] if not is_blank(data.get(f))]
                placeholders = ', '.join(['?'] * len(fields))
                values = [data[f] for f in fields]
                sql = f"INSERT INTO {table} ({', '.join(fields)}) VALUES ({placeholders})"
                lastrowid = conn.execute(sql, values).lastrowid
                row = row_to_dict(conn.execute(f"SELECT * FROM {table} WHERE id = ?", (lastrowid,)).fetchone())
                write_audit(conn, 'CREATE', table, lastrowid, new_values=row)
                conn.commit()
                return jsonify(row), 201
        except ValueError as e:
            return jsonify({'error': str(e)}), 400
        except Exception as e:
            logger.error(f"Error creating {table}: {e}")
            return jsonify({'error': f'Failed to create {table}'}), 500

    def _get(item_id):
        try:
            with get_db() as conn:
                row = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (item_id,)).fetchone()
                if not row:
                    return jsonify({'error': f'{table.rstrip("s").title()} not found'}), 404
                result = row_to_dict(row)
                if config.get('join_member'):
                    member = conn.execute("SELECT surname, given_name FROM members WHERE id = ?", (result.get('member_id'),)).fetchone()
                    result['member_surname'] = member['surname'] if member else ''
                    result['member_given_name'] = member['given_name'] if member else ''
                if config.get('join_staff'):
                    staff = conn.execute("SELECT surname, given_name FROM staff WHERE id = ?", (result.get('staff_id'),)).fetchone()
                    result['staff_surname'] = staff['surname'] if staff else ''
                    result['staff_given_name'] = staff['given_name'] if staff else ''
                return jsonify(result)
        except Exception as e:
            logger.error(f"Error fetching {table} {item_id}: {e}")
            return jsonify({'error': f'Failed to fetch {table}'}), 500

    def _update(item_id):
        try:
            data = get_json_body()
            # Blank clears an optional field; a required field cannot be cleared
            validate_required_fields(data, [f for f in config['required'] if f in data])
            with get_db() as conn:
                fields = config['fields']
                set_clauses = []
                values = []
                for f in fields:
                    if f in data:
                        set_clauses.append(f"{f}=?")
                        values.append(None if is_blank(data[f]) else data[f])
                if not set_clauses:
                    return jsonify({'error': 'No fields provided for update'}), 400
                old = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (item_id,)).fetchone()
                if not old:
                    return jsonify({'error': f'{table.rstrip("s").title()} not found'}), 404
                changed = [f for f in fields if f in data]
                if config.get('has_updated_at', True):
                    set_clauses.append("updated_at=CURRENT_TIMESTAMP")
                values.append(item_id)
                sql = f"UPDATE {table} SET {', '.join(set_clauses)} WHERE id=?"
                conn.execute(sql, values)
                write_audit(conn, 'UPDATE', table, item_id,
                            old_values={f: old[f] for f in changed}, new_values={f: None if is_blank(data[f]) else data[f] for f in changed})
                conn.commit()
                file_field = config.get('file_field')
                if file_field and file_field in data and old[file_field] != data[file_field]:
                    remove_unreferenced_document_file(conn, old[file_field])
                row = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (item_id,)).fetchone()
                return jsonify(row_to_dict(row))
        except ValueError as e:
            return jsonify({'error': str(e)}), 400
        except Exception as e:
            logger.error(f"Error updating {table} {item_id}: {e}")
            return jsonify({'error': f'Failed to update {table}'}), 500

    def _delete(item_id):
        try:
            with get_db() as conn:
                row = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (item_id,)).fetchone()
                if not row:
                    return jsonify({'error': f'{table.rstrip("s").title()} not found'}), 404
                conn.execute(f"DELETE FROM {table} WHERE id = ?", (item_id,))
                write_audit(conn, 'DELETE', table, item_id, old_values=row_to_dict(row))
                conn.commit()
                # Remove the file only after the record is gone, so a failure never orphans the row
                if config.get('file_field'):
                    remove_unreferenced_document_file(conn, row[config['file_field']])
                return jsonify({'success': True})
        except Exception as e:
            logger.error(f"Error deleting {table} {item_id}: {e}")
            return jsonify({'error': f'Failed to delete {table}'}), 500

    _list.__name__ = list_fn_name
    _create.__name__ = create_fn_name
    _get.__name__ = get_fn_name
    _update.__name__ = update_fn_name
    _delete.__name__ = delete_fn_name

    roles = config.get('roles', ANY_ROLE)
    SECTION_ROLES[url_prefix] = roles
    _list, _create, _get, _update, _delete = (require_roles(*roles)(fn) for fn in (_list, _create, _get, _update, _delete))

    app.add_url_rule(f'/api/{url_prefix}', view_func=_list, methods=['GET'])
    app.add_url_rule(f'/api/{url_prefix}', view_func=_create, methods=['POST'])
    app.add_url_rule(f'/api/{url_prefix}/<int:item_id>', view_func=_get, methods=['GET'])
    app.add_url_rule(f'/api/{url_prefix}/<int:item_id>', view_func=_update, methods=['PUT'])
    app.add_url_rule(f'/api/{url_prefix}/<int:item_id>', view_func=_delete, methods=['DELETE'])


def get_table_count(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def get_housing_by_status(conn):
    rows = conn.execute("SELECT status, COUNT(*) as count FROM housing GROUP BY status").fetchall()
    return {r['status']: r['count'] for r in rows}


def get_finances_by_category(conn):
    rows = conn.execute("SELECT category, SUM(amount) as total FROM finances WHERE type='expense' GROUP BY category ORDER BY total DESC").fetchall()
    return {r['category']: r['total'] for r in rows}


def get_infrastructure_by_condition(conn):
    rows = conn.execute("SELECT condition, COUNT(*) as count FROM infrastructure GROUP BY condition").fetchall()
    return {r['condition']: r['count'] for r in rows}


def get_tasks_by_status(conn):
    rows = conn.execute("SELECT status, COUNT(*) as count FROM tasks GROUP BY status").fetchall()
    return {r['status']: r['count'] for r in rows}


def get_notifications_by_type(conn):
    rows = conn.execute("SELECT type, COUNT(*) as count FROM notifications GROUP BY type").fetchall()
    return {r['type']: r['count'] for r in rows}


def get_staff_by_department(conn):
    rows = conn.execute("SELECT department, COUNT(*) as count FROM staff GROUP BY department ORDER BY count DESC").fetchall()
    return {r['department']: r['count'] for r in rows}


@app.errorhandler(InternalServerError)
def internal_error(e):
    # Flask has already logged the traceback; keep API errors in the JSON shape the frontend reads
    if request.path.startswith('/api/'):
        return jsonify({'error': 'Internal server error'}), 500
    return e


@app.before_request
def authenticate():
    g.user = None
    user_id = session.get('user_id')
    if user_id:
        with get_db() as conn:
            row = conn.execute("SELECT id, username, role, is_active, session_version FROM users WHERE id = ?", (user_id,)).fetchone()
        # Deactivation and password/role changes bump session_version, ending existing sessions
        if row and row['is_active'] and row['session_version'] == session.get('session_version'):
            g.user = dict(row)
        else:
            session.clear()
    # CORS preflights carry no cookies and return no data
    if request.method == 'OPTIONS':
        return None
    if g.user is None and request.endpoint not in PUBLIC_ENDPOINTS:
        return jsonify({'error': 'Authentication required'}), 401
    # Every write needs the session's token, sign-in included (it gets one from /api/auth/csrf)
    if request.method not in ('GET', 'HEAD'):
        token = session.get('csrf_token')
        if not token and app.config['SESSION_COOKIE_SECURE'] and not request.is_secure:
            # Browsers drop Secure cookies on plain http (except, in most browsers, localhost)
            logger.warning("Session cookie not returned over plain http; use HTTPS or set BAND_OFFICE_COOKIE_SECURE=0")
            return jsonify({'error': 'Your browser did not keep the sign-in cookie because this page is not served over HTTPS. '
                                     'Use the HTTPS address, or ask the administrator to set BAND_OFFICE_COOKIE_SECURE=0.'}), 403
        if not token or not hmac.compare_digest(request.headers.get('X-CSRF-Token', ''), token):
            return jsonify({'error': 'Missing or invalid CSRF token'}), 403
    return None


@app.route('/')
def index():
    return send_from_directory('templates', 'index.html')


@app.route('/static/<path:path>')
def send_static(path):
    return send_from_directory('static', path)


@app.route('/favicon.ico')
def favicon():
    return send_from_directory('static', 'favicon.ico')


@app.route('/health')
def health():
    return jsonify({'status': 'ok'}), 200


DASHBOARD_WIDGET_SECTIONS = {
    'members': 'members',
    'staff': 'staff',
    'staff_by_department': 'staff',
    'finances_by_category': 'finances',
    'recent_finances': 'finances',
}


@app.route('/api/dashboard', methods=['GET'])
def get_dashboard():
    try:
        with get_db() as conn:
            stats = {
                'members': get_table_count(conn, 'members'),
                'staff': get_table_count(conn, 'staff'),
                'housing_units': get_table_count(conn, 'housing'),
                'active_programs': conn.execute("SELECT COUNT(*) FROM programs WHERE status='active'").fetchone()[0],
                'pending_council': conn.execute("SELECT COUNT(*) FROM council WHERE status='pending'").fetchone()[0],
                'total_budget': conn.execute("SELECT COALESCE(SUM(budget),0) FROM programs").fetchone()[0] or 0,
                'total_spent': conn.execute("SELECT COALESCE(SUM(spent),0) FROM programs").fetchone()[0] or 0,
                'recent_resolutions': [],
                'recent_finances': [],
                'housing_by_status': get_housing_by_status(conn),
                'finances_by_category': get_finances_by_category(conn),
                'infrastructure_by_condition': get_infrastructure_by_condition(conn),
                'tasks_by_status': get_tasks_by_status(conn),
                'notifications_by_type': get_notifications_by_type(conn),
                'staff_by_department': get_staff_by_department(conn),
            }
            stats['recent_resolutions'] = query_all(conn, "SELECT id, item_type, title, date, status FROM council ORDER BY date DESC LIMIT 5")
            stats['recent_finances'] = query_all(conn, "SELECT id, category, type, amount, date, description FROM finances ORDER BY date DESC LIMIT 5")
            # Leave out widgets drawn from sections this role can't open
            for key, section in DASHBOARD_WIDGET_SECTIONS.items():
                if not has_role(g.user, SECTION_ROLES[section]):
                    del stats[key]
            return jsonify(stats)
    except Exception as e:
        logger.error(f"Error building dashboard: {e}")
        return jsonify({'error': 'Failed to load dashboard'}), 500


@app.route('/api/dashboard/pending-approvals')
def dashboard_pending_approvals():
    try:
        with get_db() as conn:
            leave_count = conn.execute("SELECT COUNT(*) FROM leave_requests WHERE status='pending'").fetchone()[0]
            purchase_count = conn.execute("SELECT COUNT(*) FROM purchase_requisitions WHERE status='pending'").fetchone()[0]
            meeting_count = conn.execute("SELECT COUNT(*) FROM meeting_requests WHERE status='pending'").fetchone()[0]
            incident_open = conn.execute("SELECT COUNT(*) FROM incident_reports WHERE status='open'").fetchone()[0]
            return jsonify({
                'leave_requests': leave_count,
                'purchase_requisitions': purchase_count,
                'meeting_requests': meeting_count,
                'open_incidents': incident_open,
                'total_pending': leave_count + purchase_count + meeting_count + incident_open
            })
    except Exception as e:
        logger.error(f"Error loading pending approvals: {e}")
        return jsonify({'error': 'Failed to load pending approvals'}), 500


@app.route('/api/documents/upload', methods=['POST'])
def upload_document_file():
    try:
        if 'file' not in request.files:
            return jsonify({'error': 'No file provided'}), 400
        file = request.files['file']
        if file.filename == '':
            return jsonify({'error': 'No file selected'}), 400

        _, ext = os.path.splitext(file.filename.lower())
        if ext not in ALLOWED_DOCUMENT_EXTENSIONS:
            return jsonify({'error': f'File type {ext} not allowed'}), 400

        os.makedirs(DOCUMENTS_DIR, exist_ok=True)
        # Unique prefix so uploads never overwrite each other; secure_filename strips path components
        # Reattach the validated ext: secure_filename can drop it (e.g. non-ASCII names)
        stem = os.path.splitext(file.filename)[0]
        filename = f"{uuid.uuid4().hex}_{secure_filename(stem)}{ext}"
        file_path = os.path.join('documents', filename)
        file.save(os.path.join(DOCUMENTS_DIR, filename))
        logger.info(f"Uploaded document: {filename}")
        with get_db() as conn:
            sweep_orphan_uploads(conn)
        return jsonify({'file_path': file_path}), 200
    except Exception as e:
        logger.error(f"Error uploading file: {e}")
        return jsonify({'error': 'Failed to upload file'}), 500


@app.route('/api/documents/<int:doc_id>/file')
def serve_document_file(doc_id):
    try:
        with get_db() as conn:
            row = conn.execute("SELECT file_path, title FROM documents WHERE id = ?", (doc_id,)).fetchone()
            if not row or not row['file_path']:
                return jsonify({'error': 'File not found'}), 404
            # file_path is client-supplied, so only serve files inside DOCUMENTS_DIR
            candidate = resolve_document_path(row['file_path'])
            if not candidate or not os.path.isfile(candidate):
                return jsonify({'error': 'File not found on server'}), 404
            # Only render allowed upload types inline; anything else (e.g. .html) downloads
            inline = os.path.splitext(candidate)[1].lower() in ALLOWED_DOCUMENT_EXTENSIONS
            response = send_file(candidate, as_attachment=not inline)
            response.headers['X-Content-Type-Options'] = 'nosniff'
            return response
    except Exception as e:
        logger.error(f"Error serving file for document {doc_id}: {e}")
        return jsonify({'error': 'Failed to serve file'}), 500


@app.route('/api/export/members.csv')
@require_roles('registry')
def export_members_csv():
    try:
        with get_db() as conn:
            rows = conn.execute("SELECT * FROM members ORDER BY surname, given_name").fetchall()
            output = io.StringIO()
            writer = csv.writer(output)
            writer.writerow([desc[0] for desc in conn.execute("SELECT * FROM members LIMIT 1").description])
            for row in rows:
                writer.writerow(list(row))
            output.seek(0)
            return send_file(io.BytesIO(output.getvalue().encode('utf-8')), mimetype='text/csv', as_attachment=True, download_name='members.csv')
    except Exception as e:
        logger.error(f"Error exporting members CSV: {e}")
        return jsonify({'error': 'Failed to export members'}), 500


@app.route('/api/export/finances.csv')
@require_roles('finance')
def export_finances_csv():
    try:
        with get_db() as conn:
            rows = conn.execute("SELECT * FROM finances ORDER BY date DESC").fetchall()
            output = io.StringIO()
            writer = csv.writer(output)
            writer.writerow([desc[0] for desc in conn.execute("SELECT * FROM finances LIMIT 1").description])
            for row in rows:
                writer.writerow(list(row))
            output.seek(0)
            return send_file(io.BytesIO(output.getvalue().encode('utf-8')), mimetype='text/csv', as_attachment=True, download_name='finances.csv')
    except Exception as e:
        logger.error(f"Error exporting finances CSV: {e}")
        return jsonify({'error': 'Failed to export finances'}), 500


@app.route('/api/notifications/mark-all-read', methods=['POST'])
def mark_all_notifications_read():
    try:
        with get_db() as conn:
            conn.execute("UPDATE notifications SET is_read=1 WHERE is_read=0")
            conn.commit()
            return jsonify({'success': True})
    except Exception as e:
        logger.error(f"Error marking notifications read: {e}")
        return jsonify({'error': 'Failed to mark notifications as read'}), 500


@app.route('/api/audit-log', methods=['GET'])
@require_roles()
def get_audit_log():
    try:
        with get_db() as conn:
            table_name = request.args.get('table', '')
            query = "SELECT * FROM audit_log WHERE 1=1"
            params = []
            if table_name:
                query += " AND table_name = ?"
                params.append(table_name)
            query += " ORDER BY created_at DESC, id DESC LIMIT 100"
            rows = conn.execute(query, params).fetchall()
            return jsonify([row_to_dict(r) for r in rows])
    except Exception as e:
        logger.error(f"Error fetching audit log: {e}")
        return jsonify({'error': 'Failed to fetch audit log'}), 500


@app.route('/api/reports/budget-vs-actual')
@require_roles('finance')
def report_budget_vs_actual():
    try:
        with get_db() as conn:
            rows = conn.execute("""
                SELECT p.name, p.budget, p.spent, p.budget - p.spent as remaining,
                       CASE WHEN p.spent > p.budget THEN 'over' WHEN p.spent > p.budget * 0.9 THEN 'warning' ELSE 'ok' END as status
                FROM programs p
                ORDER BY p.name
            """).fetchall()
            return jsonify([row_to_dict(r) for r in rows])
    except Exception as e:
        logger.error(f"Error generating budget report: {e}")
        return jsonify({'error': 'Failed to generate report'}), 500


@app.route('/api/reports/program-summary')
@require_roles('finance')
def report_program_summary():
    try:
        with get_db() as conn:
            rows = conn.execute("""
                SELECT p.name, p.program_type, p.department, p.budget, p.spent,
                       p.start_date, p.end_date, p.status,
                       COUNT(DISTINCT c.id) as council_count,
                       COUNT(DISTINCT f.id) as finance_count
                FROM programs p
                LEFT JOIN council c ON c.isc_reference = 'ISC-' || p.isc_program_code
                LEFT JOIN finances f ON f.isc_grant_id = p.isc_program_code
                GROUP BY p.id
                ORDER BY p.name
            """).fetchall()
            return jsonify([row_to_dict(r) for r in rows])
    except Exception as e:
        logger.error(f"Error generating program summary: {e}")
        return jsonify({'error': 'Failed to generate report'}), 500


@app.route('/api/reports/member-stats')
@require_roles('registry')
def report_member_stats():
    try:
        with get_db() as conn:
            total = conn.execute("SELECT COUNT(*) FROM members").fetchone()[0]
            by_gender = conn.execute("SELECT gender, COUNT(*) as count FROM members GROUP BY gender").fetchall()
            by_reserve = conn.execute("SELECT reserve, COUNT(*) as count FROM members GROUP BY reserve").fetchall()
            return jsonify({
                'total': total,
                'by_gender': [row_to_dict(r) for r in by_gender],
                'by_reserve': [row_to_dict(r) for r in by_reserve]
            })
    except Exception as e:
        logger.error(f"Error generating member stats: {e}")
        return jsonify({'error': 'Failed to generate member stats'}), 500


def user_payload(user):
    return {
        'id': user['id'], 'username': user['username'], 'role': user['role'],
        'sections': sorted(name for name, roles in SECTION_ROLES.items() if has_role(user, roles)),
        'csrf_token': session.get('csrf_token'),
    }


def public_user(row):
    return {k: row[k] for k in ('id', 'username', 'role', 'is_active', 'last_login_at', 'created_at')}


def parse_active(value):
    if value in (True, 1, '1', 'true'):
        return 1
    if value in (False, 0, '0', 'false'):
        return 0
    raise ValueError("is_active must be true or false")


def validate_password(password):
    if not isinstance(password, str) or len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters")


def validate_user_changes(data, creating):
    """Validated column -> value changes for a users row (password hashed)."""
    changes = {}
    if creating or 'username' in data:
        username = data.get('username')
        if not isinstance(username, str) or not USERNAME_RE.match(username):
            raise ValueError("Username must be 3-50 letters, digits, '.', '_' or '-'")
        changes['username'] = username
    if creating or 'role' in data:
        if data.get('role') not in ROLES:
            raise ValueError(f"Role must be one of: {', '.join(ROLES)}")
        changes['role'] = data['role']
    if not is_blank(data.get('is_active')):
        changes['is_active'] = parse_active(data['is_active'])
    # Blank password on update means "keep the current one"
    if creating or data.get('password'):
        validate_password(data.get('password'))
        changes['password_hash'] = generate_password_hash(data['password'])
    return changes


def create_user(conn, data):
    changes = validate_user_changes(data, creating=True)
    try:
        user_id = conn.execute(
            f"INSERT INTO users ({', '.join(changes)}) VALUES ({', '.join('?' * len(changes))})",
            list(changes.values())).lastrowid
    except sqlite3.IntegrityError:
        raise ValueError("Username already exists")
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    write_audit(conn, 'CREATE', 'users', user_id, new_values=public_user(row))
    return row


@app.route('/api/auth/csrf')
def login_csrf_token():
    # Another site can't read this response, so it can't sign a browser in to an account it controls
    session.setdefault('csrf_token', secrets.token_urlsafe(32))
    return jsonify({'csrf_token': session['csrf_token']})


def audit_failed_login(conn, user, reason):
    details = {'reason': reason, 'ip': request.remote_addr}
    if user:
        # Bounded: each IP gets MAX_FAILED_LOGINS tries per account per window
        write_audit(conn, 'LOGIN_FAILED', 'users', user['id'], new_values=details, user_name=user['username'])
        return
    # Never store the attempted name: people sometimes type a password into it
    since = f"-{int(UNKNOWN_USER_AUDIT_INTERVAL.total_seconds())} seconds"
    if not conn.execute("SELECT 1 FROM audit_log WHERE action = 'LOGIN_FAILED' AND record_id IS NULL AND created_at > datetime('now', ?)",
                        (since,)).fetchone():
        details['note'] = 'further unknown-username failures within a minute are not logged'
        write_audit(conn, 'LOGIN_FAILED', 'users', None, new_values=details, user_name='(unknown)')


def login_blocked(conn, ip, username_key):
    window = f"-{int(LOCKOUT.total_seconds())} seconds"
    conn.execute("DELETE FROM login_failures WHERE failed_at <= datetime('now', ?)", (window,))
    per_account = conn.execute("SELECT COUNT(*) FROM login_failures WHERE ip = ? AND username_hash = ?",
                               (ip, username_key)).fetchone()[0]
    per_ip = conn.execute("SELECT COUNT(*) FROM login_failures WHERE ip = ?", (ip,)).fetchone()[0]
    return per_account >= MAX_FAILED_LOGINS or per_ip >= MAX_FAILED_LOGINS_PER_IP


_warned_unconfigured_proxy = False


@app.route('/api/auth/login', methods=['POST'])
def login():
    global _warned_unconfigured_proxy
    try:
        data = get_json_body()
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    username = str(data.get('username') or '')
    password = str(data.get('password') or '')
    ip = request.remote_addr or ''
    if not PROXY_COUNT and 'X-Forwarded-For' in request.headers and not _warned_unconfigured_proxy:
        logger.warning("Requests arrive through a proxy but BAND_OFFICE_PROXY_COUNT is not set; "
                       "sign-in limits will treat all users as one IP")
        _warned_unconfigured_proxy = True
    # Hashed: the attempted name may be a mistyped password
    username_key = hashlib.sha256(username.encode()).hexdigest()
    with get_db() as conn:
        # Blocked, wrong and unknown all get the same answer, so responses don't reveal which accounts exist
        if login_blocked(conn, ip, username_key):
            conn.commit()
            return jsonify({'error': INVALID_LOGIN}), 401
        user = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        valid = check_password_hash(user['password_hash'] if user else DUMMY_PASSWORD_HASH, password)
        if not (user and valid and user['is_active']):
            conn.execute("INSERT INTO login_failures (ip, username_hash) VALUES (?, ?)", (ip, username_key))
            if user:
                audit_failed_login(conn, user, 'wrong password' if not valid else 'inactive account')
            else:
                audit_failed_login(conn, None, 'unknown username')
            if login_blocked(conn, ip, username_key):  # this failure reached a limit; logged once per block
                write_audit(conn, 'LOCKOUT', 'users', user['id'] if user else None,
                            new_values={'ip': ip, 'minutes': int(LOCKOUT.total_seconds() // 60)},
                            user_name=user['username'] if user else '(unknown)')
            conn.commit()
            return jsonify({'error': INVALID_LOGIN}), 401
        conn.execute("DELETE FROM login_failures WHERE ip = ? AND username_hash = ?", (ip, username_key))
        conn.execute("UPDATE users SET last_login_at = CURRENT_TIMESTAMP WHERE id = ?", (user['id'],))
        write_audit(conn, 'LOGIN', 'users', user['id'], new_values={'ip': ip}, user_name=user['username'])
        conn.commit()
    # New session on login so a pre-login session id can't be reused
    session.clear()
    session.permanent = True
    session['user_id'] = user['id']
    session['session_version'] = user['session_version']
    session['csrf_token'] = secrets.token_urlsafe(32)
    return jsonify(user_payload(user))


@app.route('/api/auth/me')
def current_user():
    return jsonify(user_payload(g.user))


@app.route('/api/auth/logout', methods=['POST'])
def logout():
    with get_db() as conn:
        write_audit(conn, 'LOGOUT', 'users', g.user['id'])
        conn.commit()
    session.clear()
    return jsonify({'success': True})


@app.route('/api/auth/password', methods=['POST'])
def change_own_password():
    try:
        data = get_json_body()
        with get_db() as conn:
            row = conn.execute("SELECT password_hash FROM users WHERE id = ?", (g.user['id'],)).fetchone()
            if not check_password_hash(row['password_hash'], str(data.get('current_password') or '')):
                raise ValueError("Current password is incorrect")
            validate_password(data.get('new_password'))
            conn.execute("UPDATE users SET password_hash = ?, session_version = session_version + 1 WHERE id = ?",
                         (generate_password_hash(data['new_password']), g.user['id']))
            write_audit(conn, 'UPDATE', 'users', g.user['id'], new_values={'password': '(changed)'})
            conn.commit()
            # Other sessions end; keep this one
            session['session_version'] = g.user['session_version'] + 1
            return jsonify({'success': True})
    except ValueError as e:
        return jsonify({'error': str(e)}), 400


@app.route('/api/users', methods=['GET'])
@require_roles()
def list_users():
    with get_db() as conn:
        return jsonify([public_user(r) for r in conn.execute("SELECT * FROM users ORDER BY username")])


@app.route('/api/users/<int:user_id>', methods=['GET'])
@require_roles()
def get_user(user_id):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return jsonify(public_user(row)) if row else (jsonify({'error': 'User not found'}), 404)


@app.route('/api/users', methods=['POST'])
@require_roles()
def create_user_route():
    try:
        data = get_json_body()
        with get_db() as conn:
            row = create_user(conn, data)
            conn.commit()
            return jsonify(public_user(row)), 201
    except ValueError as e:
        return jsonify({'error': str(e)}), 400


@app.route('/api/users/<int:user_id>', methods=['PUT'])
@require_roles()
def update_user(user_id):
    try:
        data = get_json_body()
        changes = validate_user_changes(data, creating=False)
        with get_db() as conn:
            old = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            if not old:
                return jsonify({'error': 'User not found'}), 404
            if not changes:
                return jsonify({'error': 'No fields provided for update'}), 400
            sets = [f"{k} = ?" for k in changes]
            if 'password_hash' in changes or any(k in changes and changes[k] != old[k] for k in ('role', 'is_active')):
                sets.append("session_version = session_version + 1")  # sign the user out everywhere
            try:
                conn.execute(f"UPDATE users SET {', '.join(sets)} WHERE id = ?", [*changes.values(), user_id])
            except sqlite3.IntegrityError:
                raise ValueError("Username already exists")
            if not conn.execute("SELECT 1 FROM users WHERE role = 'admin' AND is_active = 1").fetchone():
                conn.rollback()
                raise ValueError("At least one active admin is required")
            audited = {k: v for k, v in changes.items() if k != 'password_hash'}
            if 'password_hash' in changes:
                audited['password'] = '(changed)'
            write_audit(conn, 'UPDATE', 'users', user_id,
                        old_values={k: old[k] for k in audited if k != 'password'}, new_values=audited)
            conn.commit()
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            return jsonify(public_user(row))
    except ValueError as e:
        return jsonify({'error': str(e)}), 400


@app.cli.command('create-user')
@click.argument('username')
@click.option('--role', type=click.Choice(ROLES), default='admin', show_default=True)
@click.password_option()
def create_user_command(username, role, password):
    """Create a login, e.g. the first admin: flask --app app create-user alice"""
    init_db()
    try:
        with get_db() as conn:
            create_user(conn, {'username': username, 'role': role, 'password': password})
            conn.commit()
    except ValueError as e:
        raise click.ClickException(str(e))
    click.echo(f"Created {role} user {username}")


def seed_members(conn):
    members = [
        ("B001-001", "Smith", "John", "A", "1980-05-15", "M", "123456789", 1, "Main Reserve", "123 Band Office Rd", "Vancouver", "BC", "V6B 1A1", "604-555-0101", "john.smith@example.ca", None, "2010-03-15", None, None, "Elder member"),
        ("B001-002", "Smith", "Mary", "B", "1982-08-22", "F", "123456790", 1, "Main Reserve", "456 Community Lane", "Vancouver", "BC", "V6B 1A2", "604-555-0102", "mary.smith@example.ca", None, "2010-03-15", None, None, "Band Councillor"),
        ("B001-003", "Bear", "Thomas", "C", "1975-12-01", "M", "123456791", 1, "Main Reserve", "789 Elder St", "Vancouver", "BC", "V6B 1A3", "604-555-0103", "thomas.bear@example.ca", None, "2005-06-20", None, None, "Band Manager"),
        ("B001-004", "Eagle", "Sarah", "D", "1990-03-10", "F", "123456792", 1, "Main Reserve", "321 Youth Ave", "Vancouver", "BC", "V6B 1A4", "604-555-0104", "sarah.eagle@example.ca", None, "2015-09-01", None, None, "Youth coordinator"),
        ("B001-005", "Wolf", "Daniel", "E", "1995-07-18", "M", "123456793", 1, "Main Reserve", "654 Main St", "Vancouver", "BC", "V6B 1A5", "604-555-0105", "daniel.wolf@example.ca", None, "2018-01-10", None, None, "Housing applicant"),
    ]
    for m in members:
        conn.execute("INSERT INTO members (band_number, surname, given_name, middle_name, dob, gender, status_number, is_register, reserve, address, city, province, postal_code, phone, email, guardian, membership_date, transfer_from, transfer_date, notes) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", m)


def seed_staff(conn):
    staff = [
        ("Bear", "Thomas", "Band Manager", "Administration", "2018-01-15", None, "active", "604-555-0103", "thomas.bear@example.ca", 95000, 1, "Manages day-to-day operations under Chief and Council"),
        ("Eagle", "Sarah", "Finance Officer", "Finance", "2020-06-01", None, "active", "604-555-0104", "sarah.eagle@example.ca", 72000, 1, "Handles ISC funding and band finances"),
        ("Wolf", "Daniel", "Housing Coordinator", "Housing", "2021-03-10", None, "active", "604-555-0105", "daniel.wolf@example.ca", 65000, 1, "Manages on-reserve housing and ISC housing programs"),
        ("Smith", "Mary", "Band Councillor", "Council", "2022-10-15", None, "active", "604-555-0102", "mary.smith@example.ca", 45000, 1, "Elected council member"),
        ("Little", "Anna", "Registry Clerk", "Membership", "2023-01-20", None, "active", "604-555-0106", "anna.little@example.ca", 55000, 1, "Handles Indian Register and status card applications"),
    ]
    for s in staff:
        conn.execute("INSERT INTO staff (surname, given_name, position, department, start_date, end_date, status, phone, email, salary, benefits_eligible, notes) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", s)


def seed_housing(conn):
    housing = [
        ("123 Band Office Rd", "Single Family", 3, 2, 4, 1, "occupied", "good", "New roof 2024", "2020-05-01", 1),
        ("456 Community Lane", "Single Family", 4, 3, 6, 2, "occupied", "good", None, "2019-08-15", 1),
        ("789 Elder St", "Senior", 2, 1, 1, 3, "occupied", "fair", "Needs furnace repair", "2018-11-20", 1),
        ("321 Youth Ave", "Apartment", 2, 1, 2, 4, "occupied", "good", None, "2021-02-10", 1),
        ("654 Main St", "Single Family", 3, 2, 3, None, "vacant", "good", None, None, 1),
        ("100 Waterfront Dr", "Single Family", 4, 3, 5, None, "needs-repair", "poor", "Mold remediation needed - ISC housing funding required", None, 1),
    ]
    for h in housing:
        conn.execute("INSERT INTO housing (address, unit_type, bedrooms, bathrooms, occupants, member_id, status, condition, maintenance_notes, assigned_date, isc_funding_eligible) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", h)


def seed_programs(conn):
    programs = [
        ("Band Support Funding", "Governance", "Administration", "ISC Band Support Funding", 450000, 320000, "2025-04-01", "2026-03-31", "active", "Core funding for band government operations and administration of ISC programs", "BSF-2025-001", "Annual report to ISC regional office"),
        ("On-Reserve Housing Program", "Housing", "Housing", "ISC First Nations Housing", 1200000, 850000, "2025-01-01", "2026-12-31", "active", "Construction and renovation of on-reserve housing units", "ORH-2025-003", "Quarterly financial and progress reports to ISC"),
        ("Jordan's Principle", "Child & Family", "Health", "ISC First Nations Child & Family Services", 180000, 145000, "2025-01-01", "2026-03-31", "active", "Ensures First Nations children have equitable access to services", "JDP-2025-002", "Monthly case tracking to ISC"),
        ("Post-Secondary Education", "Education", "Education", "ISC Post-Secondary Education", 250000, 180000, "2025-09-01", "2026-08-31", "active", "Distinctions-based post-secondary support for band members", "PSE-2025-004", "Annual student tracking report"),
        ("Income Assistance", "Social Programs", "Social Development", "ISC Income Assistance", 320000, 280000, "2025-04-01", "2026-03-31", "active", "On-reserve income assistance program", "IA-2025-005", "Monthly case counts and expenditure reports"),
        ("Emergency Management", "Infrastructure", "Public Works", "ISC Emergency Management", 50000, 12000, "2025-01-01", "2026-03-31", "active", "Community emergency preparedness and response", "EMP-2025-006", "After-action reports required"),
        ("Lands and Economic Development", "Economic Dev", "Lands", "ISC Lands & Economic Development", 75000, 45000, "2025-04-01", "2026-03-31", "active", "Land use planning and economic development initiatives", "LED-2025-007", "Annual progress report to ISC"),
    ]
    for p in programs:
        conn.execute("INSERT INTO programs (name, program_type, department, funding_source, budget, spent, start_date, end_date, status, description, isc_program_code, reporting_requirements) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", p)


def seed_council(conn):
    council = [
        ("resolution", "Approval of 2025-2026 Annual Budget", "2025-03-15", "Chief, Councillors Smith, Bear, Eagle", "BE IT RESOLVED that the 2025-2026 Annual Budget be approved as presented.", "Moved by Councillor Smith, seconded by Councillor Bear", 3, 0, 0, "approved", "Reviewed by Band Manager", 1, "ISC-BSF-2025-001", "Budget submitted to ISC regional office"),
        ("resolution", "ISC Housing Funding Application - Unit Renovations", "2025-04-02", "Chief, Councillors Smith, Bear, Eagle, Wolf", "BE IT RESOLVED that the Band Manager submit a housing renovation application to ISC for 6 units.", "Moved by Chief, seconded by Councillor Wolf", 4, 0, 0, "approved", "Application prepared", 1, "ISC-ORH-2025-003", "Submitted April 5, 2025"),
        ("by-law", "Land Use Zoning By-law No. 2025-01", "2025-02-20", "Chief, Councillors Smith, Bear", "A by-law to regulate land use and zoning within the reserve boundaries.", "Moved by Councillor Bear, seconded by Councillor Smith", 2, 1, 0, "approved", "Legal review completed", 1, None, "Published in community newsletter"),
        ("meeting", "Monthly Council Meeting - March 2025", "2025-03-15", "Chief, All Councillors, Band Manager, Registry Clerk", "Regular monthly council meeting. Discussed budget, housing, and ISC reporting.", None, None, None, None, "completed", None, 0, None, "Minutes posted in band office"),
        ("resolution", "New Fiscal Relationship Grant Agreement", "2025-05-10", "Chief, All Councillors", "BE IT RESOLVED that the Band enter into a New Fiscal Relationship Grant agreement with ISC.", "Moved by Chief, seconded by Councillor Eagle", 4, 0, 0, "approved", "Agreement under review by legal counsel", 0, None, "Pending ISC response"),
    ]
    for c in council:
        conn.execute("INSERT INTO council (item_type, title, date, attendees, resolution_text, motion, vote_for, vote_against, abstain, status, band_manager_review, isc_submitted, isc_reference, notes) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", c)


def seed_finances(conn):
    finances = [
        ("Band Support Funding", "income", 450000, "2025-04-01", "Annual Band Support Funding grant from ISC", "ISC", "BSF-2025-Q1", "BSF-2025-001", "Thomas Bear"),
        ("Housing Renovation Grant", "income", 800000, "2025-04-15", "First phase of housing renovation funding", "ISC", "ORH-2025-P1", "ORH-2025-003", "Sarah Eagle"),
        ("Staff Salaries", "expense", 45000, "2025-04-30", "April payroll - Administration", "ISC BSF", "BSF-2025-APR", "BSF-2025-001", "Thomas Bear"),
        ("Housing Materials", "expense", 35000, "2025-05-10", "Construction materials for housing renovations", "ISC ORH", "ORH-2025-MAT", "ORH-2025-003", "Daniel Wolf"),
        ("Jordan's Principle Cases", "expense", 25000, "2025-05-15", "Child and family services expenditures", "ISC JDP", "JDP-2025-MAY", "JDP-2025-002", "Sarah Eagle"),
        ("Education Bursaries", "expense", 15000, "2025-06-01", "Post-secondary education support payments", "ISC PSE", "PSE-2025-Q1", "PSE-2025-004", "Anna Little"),
        ("Emergency Management Equipment", "expense", 8000, "2025-06-15", "Emergency response equipment purchase", "ISC EMP", "EMP-2025-EQ", "EMP-2025-006", "Thomas Bear"),
        ("Community Event", "expense", 5000, "2025-06-20", "National Indigenous Peoples Day celebration", "Band", "EVT-2025-001", None, "Thomas Bear"),
    ]
    for f in finances:
        conn.execute("INSERT INTO finances (category, type, amount, date, description, funding_source, reference_number, isc_grant_id, approved_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", f)


def seed_infrastructure(conn):
    infrastructure = [
        ("Water Treatment Plant", "Water", "North End", "good", 2500000, "2025-01-15", "ISC Infrastructure Fund", "Annual inspection and maintenance", "Long-term drinking water advisory lifted 2024"),
        ("Main Community Centre", "Building", "Central", "good", 1800000, "2024-09-20", "ISC Infrastructure Fund", "Bi-annual inspection", "Houses band office, gym, and community hall"),
        ("Public Works Garage", "Building", "Industrial Area", "fair", 450000, "2025-02-10", None, "Annual maintenance", "Needs roof repair estimated $50,000"),
        ("Fire Hall", "Building", "Central", "good", 320000, "2025-03-05", "ISC Fire Protection", "Annual inspection", "Staffed by 8 volunteer firefighters"),
        ("Road Network - Main Reserve", "Roads", "Throughout Reserve", "fair", 1200000, "2024-08-30", "ISC Infrastructure", "Spring grading and summer paving", "25km of reserve roads"),
        ("Wastewater Treatment Facility", "Water", "South End", "good", 2100000, "2025-01-20", "ISC Infrastructure Fund", "Quarterly monitoring", "Upgraded 2023 with ISC funding"),
        ("Youth Centre", "Building", "Main Reserve", "good", 280000, "2024-11-15", None, "Monthly cleaning and maintenance", "Programs run Mon-Fri 3pm-8pm"),
    ]
    for i in infrastructure:
        conn.execute("INSERT INTO infrastructure (name, asset_type, location, condition, estimated_value, last_inspection, isc_funding_program, maintenance_schedule, notes) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", i)


def seed_notifications(conn):
    notifications = [
        ("ISC Quarterly Report Due", "Band Support Funding quarterly report due to ISC regional office by 2025-07-15", "deadline", "high", "programs", 1),
        ("Housing Inspection Scheduled", "Annual housing inspection scheduled for 2025-07-20 at 10:00 AM", "info", "normal", "housing", 1),
        ("Council Meeting Follow-up", "Action items from June council meeting need follow-up", "action", "medium", "council", 4),
        ("Jordan's Principle Case Review", "Monthly case review meeting scheduled for 2025-07-10", "deadline", "high", "programs", 3),
        ("ISC Grant Agreement Pending", "New Fiscal Relationship Grant agreement awaiting ISC response", "info", "medium", "council", 5),
    ]
    for n in notifications:
        conn.execute("INSERT INTO notifications (title, message, type, priority, related_table, related_id) VALUES (?, ?, ?, ?, ?, ?)", n)


def seed_tasks(conn):
    tasks = [
        ("Submit ISC Quarterly Report", "Compile and submit Band Support Funding quarterly report", "pending", "high", "Sarah Eagle", "2025-07-15", "programs", 1),
        ("Review Housing Applications", "Review 3 pending housing applications from band members", "pending", "medium", "Daniel Wolf", "2025-07-12", "housing", 1),
        ("Update Council Minutes", "Post June council minutes on band office bulletin board", "completed", "low", "Anna Little", "2025-07-01", "council", 4),
        ("Jordan's Principle Monthly Report", "Submit monthly case counts to ISC", "pending", "high", "Sarah Eagle", "2025-07-10", "programs", 3),
        ("Follow up on ISC Grant", "Follow up with ISC regional office on grant agreement", "pending", "medium", "Thomas Bear", "2025-07-08", "council", 5),
        ("Water Treatment Inspection", "Schedule annual water treatment plant inspection", "pending", "medium", "Daniel Wolf", "2025-07-25", "infrastructure", 1),
    ]
    for t in tasks:
        conn.execute("INSERT INTO tasks (title, description, status, priority, assigned_to, due_date, related_table, related_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", t)


def seed_documents(conn):
    documents = [
        ("BSF Quarterly Report Q1 2025", "report", "programs", 1, "/reports/bsf-q1-2025.pdf", "Band Support Funding quarterly report", "Thomas Bear"),
        ("Housing Inspection Checklist", "checklist", "housing", 1, "/checklists/housing-inspection.pdf", "Annual housing inspection checklist", "Daniel Wolf"),
        ("Council By-law No. 2025-01", "by-law", "council", 3, "/bylaws/by-law-2025-01.pdf", "Land Use Zoning By-law No. 2025-01", "Thomas Bear"),
        ("Jordan's Principle Policy", "policy", "programs", 3, "/policies/jdp-policy.pdf", "Jordan's Principle implementation policy", "Sarah Eagle"),
        ("ISC Grant Agreement Draft", "agreement", "council", 5, "/agreements/nfr-draft.pdf", "New Fiscal Relationship Grant agreement draft", "Thomas Bear"),
    ]
    for d in documents:
        conn.execute("INSERT INTO documents (title, document_type, related_table, related_id, file_path, content, created_by) VALUES (?, ?, ?, ?, ?, ?, ?)", d)


def seed_audit_log(conn):
    audit_log = [
        ("CREATE", "members", 1, None, "Created member John Smith (B001-001)", "system", "2025-01-01"),
        ("CREATE", "staff", 1, None, "Created staff Thomas Bear - Band Manager", "system", "2025-01-01"),
        ("UPDATE", "programs", 1, "Program spent: 300000", "Program spent updated from 300000 to 320000", "Sarah Eagle", "2025-06-15"),
        ("CREATE", "council", 1, None, "Created resolution: Approval of 2025-2026 Annual Budget", "system", "2025-03-15"),
    ]
    for a in audit_log:
        conn.execute("INSERT INTO audit_log (action, table_name, record_id, old_values, new_values, user_name, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)", a)


def seed_leave_requests(conn):
    leave_requests = [
        (1, "Annual Leave", "2025-07-14", "2025-07-18", "Family vacation", "approved", "Thomas Bear", "2025-07-01"),
        (2, "Sick Leave", "2025-07-10", "2025-07-10", "Medical appointment", "pending", None, None),
        (3, "Training Leave", "2025-08-05", "2025-08-09", "ISC training workshop in Vancouver", "pending", None, None),
    ]
    for l in leave_requests:
        conn.execute("INSERT INTO leave_requests (staff_id, leave_type, start_date, end_date, reason, status, approved_by, approved_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", l)


def seed_purchase_requisitions(conn):
    purchase_requisitions = [
        ("Office Supplies - Q3", "Printer paper, toner, pens, notebooks", 1500, "Staples", "Anna Little", "Administration", "pending", "medium", None),
        ("Laptop for Housing Coordinator", "Dell Latitude laptop for housing site visits", 1200, "Best Buy", "Daniel Wolf", "Housing", "approved", "high", "Approved by Band Manager"),
        ("Community Event Supplies", "Food, drinks, and supplies for National Indigenous Peoples Day", 3000, "Local Vendor", "Thomas Bear", "Community", "pending", "high", None),
    ]
    for p in purchase_requisitions:
        conn.execute("INSERT INTO purchase_requisitions (title, description, amount, vendor, requested_by, department, status, priority, approval_notes) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", p)


def seed_incident_reports(conn):
    incident_reports = [
        ("Slip and Fall", "Staff member slipped on ice near main entrance", "Main Entrance", "low", "Anna Little", "2025-07-05", "2025-07-05", "resolved", "Salt applied, warning signs posted", "2025-07-05"),
        ("Water Leak", "Water leak detected in youth centre washroom", "Youth Centre", "medium", "Daniel Wolf", "2025-07-08", "2025-07-08", "open", "Plumber scheduled for repair", None),
        ("Vehicle Accident", "Band vehicle involved in minor fender bender", "Parking Lot", "high", "Thomas Bear", "2025-07-10", "2025-07-10", "under-review", "Insurance notified", None),
    ]
    for i in incident_reports:
        conn.execute("INSERT INTO incident_reports (incident_type, description, location, severity, reported_by, date_reported, date_occurred, status, follow_up_actions, resolved_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", i)


def seed_meeting_requests(conn):
    meeting_requests = [
        ("Budget Review Meeting", "Review Q3 budget and approve pending requisitions", "Sarah Eagle", "2025-07-15", "09:00", "11:00", "Conference Room", "Thomas Bear, Sarah Eagle, Daniel Wolf", "pending", None, None),
        ("Housing Committee Meeting", "Monthly housing committee meeting", "Daniel Wolf", "2025-07-18", "13:00", "15:00", "Conference Room", "Thomas Bear, Daniel Wolf, Anna Little", "approved", "Thomas Bear", "2025-07-12"),
    ]
    for m in meeting_requests:
        conn.execute("INSERT INTO meeting_requests (title, description, requested_by, meeting_date, start_time, end_time, location, attendees, status, approved_by, approved_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", m)


def seed_data(conn):
    seed_members(conn)
    seed_staff(conn)
    seed_housing(conn)
    seed_programs(conn)
    seed_council(conn)
    seed_finances(conn)
    seed_infrastructure(conn)
    seed_notifications(conn)
    seed_tasks(conn)
    seed_documents(conn)
    seed_audit_log(conn)
    seed_leave_requests(conn)
    seed_purchase_requisitions(conn)
    seed_incident_reports(conn)
    seed_meeting_requests(conn)


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS members (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                band_number TEXT UNIQUE NOT NULL,
                surname TEXT NOT NULL,
                given_name TEXT NOT NULL,
                middle_name TEXT,
                dob DATE,
                gender TEXT,
                status_number TEXT,
                is_register INTEGER DEFAULT 1,
                reserve TEXT,
                address TEXT,
                city TEXT,
                province TEXT,
                postal_code TEXT,
                phone TEXT,
                email TEXT,
                guardian TEXT,
                membership_date DATE,
                transfer_from TEXT,
                transfer_date DATE,
                notes TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS staff (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                surname TEXT NOT NULL,
                given_name TEXT NOT NULL,
                position TEXT,
                department TEXT,
                start_date DATE,
                end_date DATE,
                status TEXT DEFAULT 'active',
                phone TEXT,
                email TEXT,
                salary REAL,
                benefits_eligible INTEGER DEFAULT 1,
                notes TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS housing (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                address TEXT NOT NULL,
                unit_type TEXT,
                bedrooms INTEGER,
                bathrooms INTEGER,
                occupants INTEGER DEFAULT 0,
                member_id INTEGER,
                status TEXT DEFAULT 'occupied',
                condition TEXT DEFAULT 'good',
                maintenance_notes TEXT,
                assigned_date DATE,
                isc_funding_eligible INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (member_id) REFERENCES members(id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS programs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                program_type TEXT,
                department TEXT,
                funding_source TEXT,
                budget REAL DEFAULT 0,
                spent REAL DEFAULT 0,
                start_date DATE,
                end_date DATE,
                status TEXT DEFAULT 'active',
                description TEXT,
                isc_program_code TEXT,
                reporting_requirements TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS council (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_type TEXT NOT NULL,
                title TEXT NOT NULL,
                date DATE NOT NULL,
                attendees TEXT,
                resolution_text TEXT,
                motion TEXT,
                vote_for INTEGER DEFAULT 0,
                vote_against INTEGER DEFAULT 0,
                abstain INTEGER DEFAULT 0,
                status TEXT DEFAULT 'pending',
                band_manager_review TEXT,
                isc_submitted INTEGER DEFAULT 0,
                isc_reference TEXT,
                notes TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS finances (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT NOT NULL,
                type TEXT NOT NULL,
                amount REAL NOT NULL,
                date DATE NOT NULL,
                description TEXT,
                funding_source TEXT,
                reference_number TEXT,
                isc_grant_id TEXT,
                approved_by TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS infrastructure (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                asset_type TEXT,
                location TEXT,
                condition TEXT DEFAULT 'good',
                estimated_value REAL,
                last_inspection DATE,
                isc_funding_program TEXT,
                maintenance_schedule TEXT,
                notes TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                message TEXT NOT NULL,
                type TEXT DEFAULT 'info',
                priority TEXT DEFAULT 'normal',
                related_table TEXT,
                related_id INTEGER,
                is_read INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                description TEXT,
                status TEXT DEFAULT 'pending',
                priority TEXT DEFAULT 'medium',
                assigned_to TEXT,
                due_date DATE,
                related_table TEXT,
                related_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                document_type TEXT,
                related_table TEXT,
                related_id INTEGER,
                file_path TEXT,
                content TEXT,
                created_by TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                action TEXT NOT NULL,
                table_name TEXT NOT NULL,
                record_id INTEGER,
                old_values TEXT,
                new_values TEXT,
                user_name TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS leave_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                staff_id INTEGER,
                leave_type TEXT NOT NULL,
                start_date DATE NOT NULL,
                end_date DATE NOT NULL,
                reason TEXT,
                status TEXT DEFAULT 'pending',
                approved_by TEXT,
                approved_at DATE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (staff_id) REFERENCES staff(id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS purchase_requisitions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                description TEXT,
                amount REAL NOT NULL,
                vendor TEXT,
                requested_by TEXT,
                department TEXT,
                status TEXT DEFAULT 'pending',
                priority TEXT DEFAULT 'medium',
                approval_notes TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS incident_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                incident_type TEXT NOT NULL,
                description TEXT NOT NULL,
                location TEXT,
                severity TEXT DEFAULT 'medium',
                reported_by TEXT,
                date_reported DATE NOT NULL,
                date_occurred DATE,
                status TEXT DEFAULT 'open',
                follow_up_actions TEXT,
                resolved_at DATE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS meeting_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                description TEXT,
                requested_by TEXT,
                meeting_date DATE NOT NULL,
                start_time TEXT,
                end_time TEXT,
                location TEXT,
                attendees TEXT,
                status TEXT DEFAULT 'pending',
                approved_by TEXT,
                approved_at DATE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                session_version INTEGER NOT NULL DEFAULT 0,
                last_login_at TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS login_failures (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ip TEXT NOT NULL,
                username_hash TEXT NOT NULL,
                failed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS login_failures_ip ON login_failures (ip, username_hash)")
        # Earlier versions wrote these in local time as ISO 'YYYY-MM-DDTHH:MM:SS'; store UTC like CURRENT_TIMESTAMP
        conn.execute("UPDATE audit_log SET created_at = datetime(created_at, 'utc') WHERE created_at LIKE '____-__-__T%'")
        conn.execute("UPDATE users SET last_login_at = datetime(last_login_at, 'utc') WHERE last_login_at LIKE '____-__-__T%'")
        conn.commit()

        if conn.execute("SELECT COUNT(*) FROM members").fetchone()[0] == 0:
            seed_data(conn)
            conn.commit()

        if not conn.execute("SELECT 1 FROM users").fetchone():
            logger.warning("No users exist yet. Create the first admin with: flask --app app create-user <username>")


register_crud_routes(app, 'members', 'members')
register_crud_routes(app, 'staff', 'staff')
register_crud_routes(app, 'housing', 'housing')
register_crud_routes(app, 'programs', 'programs')
register_crud_routes(app, 'council', 'council')
register_crud_routes(app, 'finances', 'finances')
register_crud_routes(app, 'infrastructure', 'infrastructure')
register_crud_routes(app, 'notifications', 'notifications')
register_crud_routes(app, 'tasks', 'tasks')
register_crud_routes(app, 'documents', 'documents')
register_crud_routes(app, 'leave_requests', 'leave-requests')
register_crud_routes(app, 'purchase_requisitions', 'purchase-requisitions')
register_crud_routes(app, 'incident_reports', 'incident-reports')
register_crud_routes(app, 'meeting_requests', 'meeting-requests')

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    init_db()
    app.run(host=HOST, port=PORT, debug=DEBUG)