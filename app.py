from flask import Flask, jsonify, request, send_from_directory, send_file
from flask_cors import CORS
import sqlite3
import os
import csv
import io
import logging
from datetime import datetime
from contextlib import contextmanager

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'data', 'band_office.db')
DOCUMENTS_DIR = os.path.join(BASE_DIR, 'documents')

app = Flask(__name__, static_folder='static', template_folder='templates')
CORS(app)

PORT = int(os.environ.get('BAND_OFFICE_PORT', '5000'))
DEBUG = os.environ.get('BAND_OFFICE_DEBUG', '0').lower() in ('1', 'true', 'yes')
SECRET_KEY = os.environ.get('BAND_OFFICE_SECRET_KEY', 'change-this-in-production')
app.secret_key = SECRET_KEY

logger = logging.getLogger(__name__)


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


def execute(conn, sql, params=()):
    cursor = conn.execute(sql, params)
    conn.commit()
    return cursor.lastrowid


def validate_required_fields(data, required_fields):
    missing = [f for f in required_fields if not data.get(f)]
    if missing:
        raise ValueError(f"Missing required fields: {', '.join(missing)}")


RESOURCE_REGISTRY = {
    'members': {
        'table': 'members',
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
    },
    'finances': {
        'table': 'finances',
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
    },
    'leave_requests': {
        'table': 'leave_requests',
        'fields': ['staff_id', 'leave_type', 'start_date', 'end_date', 'reason',
                   'status', 'approved_by', 'approved_at'],
        'required': ['staff_id', 'leave_type', 'start_date', 'end_date'],
        'search': [],
        'order_by': 'created_at DESC',
        'pagination': False,
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
                filter_value = request.args.get(config.get('filter_field', ''), '') or request.args.get('type', '') or request.args.get('status', '')
                page = int(request.args.get('page', 1))
                per_page = int(request.args.get('per_page', 50))

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
        except Exception as e:
            logger.error(f"Error listing {table}: {e}")
            return jsonify({'error': f'Failed to list {table}'}), 500

    def _create():
        try:
            data = request.json
            validate_required_fields(data, config['required'])
            with get_db() as conn:
                fields = config['fields']
                placeholders = ', '.join(['?'] * len(fields))
                values = [data.get(f) for f in fields]
                sql = f"INSERT INTO {table} ({', '.join(fields)}) VALUES ({placeholders})"
                lastrowid = execute(conn, sql, values)
                row = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (lastrowid,)).fetchone()
                return jsonify(row_to_dict(row)), 201
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
            data = request.json
            with get_db() as conn:
                fields = config['fields']
                set_clauses = []
                values = []
                for f in fields:
                    if f in data:
                        set_clauses.append(f"{f}=?")
                        values.append(data[f])
                if not set_clauses:
                    return jsonify({'error': 'No fields provided for update'}), 400
                values.append(item_id)
                sql = f"UPDATE {table} SET {', '.join(set_clauses)}, updated_at=CURRENT_TIMESTAMP WHERE id=?"
                execute(conn, sql, values)
                row = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (item_id,)).fetchone()
                if not row:
                    return jsonify({'error': f'{table.rstrip("s").title()} not found'}), 404
                return jsonify(row_to_dict(row))
        except Exception as e:
            logger.error(f"Error updating {table} {item_id}: {e}")
            return jsonify({'error': f'Failed to update {table}'}), 500

    def _delete(item_id):
        try:
            with get_db() as conn:
                row = conn.execute(f"SELECT id FROM {table} WHERE id = ?", (item_id,)).fetchone()
                if not row:
                    return jsonify({'error': f'{table.rstrip("s").title()} not found'}), 404
                conn.execute(f"DELETE FROM {table} WHERE id = ?", (item_id,))
                conn.commit()
                return jsonify({'success': True})
        except Exception as e:
            logger.error(f"Error deleting {table} {item_id}: {e}")
            return jsonify({'error': f'Failed to delete {table}'}), 500

    _list.__name__ = list_fn_name
    _create.__name__ = create_fn_name
    _get.__name__ = get_fn_name
    _update.__name__ = update_fn_name
    _delete.__name__ = delete_fn_name

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

        allowed_extensions = {'.pdf', '.doc', '.docx', '.txt', '.jpg', '.jpeg', '.png'}
        _, ext = os.path.splitext(file.filename.lower())
        if ext not in allowed_extensions:
            return jsonify({'error': f'File type {ext} not allowed'}), 400

        os.makedirs(DOCUMENTS_DIR, exist_ok=True)
        filename = file.filename
        file_path = os.path.join('documents', filename)
        file.save(os.path.join(BASE_DIR, file_path))
        logger.info(f"Uploaded document: {filename}")
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
            file_path = row['file_path']
            if os.path.isabs(file_path):
                candidate = file_path
            else:
                candidate = os.path.join(BASE_DIR, file_path)
                if not os.path.exists(candidate):
                    candidate = file_path
            if not os.path.exists(candidate):
                return jsonify({'error': 'File not found on server'}), 404
            return send_file(candidate, as_attachment=False)
    except Exception as e:
        logger.error(f"Error serving file for document {doc_id}: {e}")
        return jsonify({'error': 'Failed to serve file'}), 500


@app.route('/api/export/members.csv')
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
def get_audit_log():
    try:
        with get_db() as conn:
            table_name = request.args.get('table', '')
            query = "SELECT * FROM audit_log WHERE 1=1"
            params = []
            if table_name:
                query += " AND table_name = ?"
                params.append(table_name)
            query += " ORDER BY created_at DESC LIMIT 100"
            rows = conn.execute(query, params).fetchall()
            return jsonify([row_to_dict(r) for r in rows])
    except Exception as e:
        logger.error(f"Error fetching audit log: {e}")
        return jsonify({'error': 'Failed to fetch audit log'}), 500


@app.route('/api/audit-log', methods=['POST'])
def create_audit_log():
    try:
        data = request.json
        with get_db() as conn:
            cursor = conn.execute(
                "INSERT INTO audit_log (action, table_name, record_id, old_values, new_values, user_name, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (data.get('action'), data.get('table_name'), data.get('record_id'), data.get('old_values'),
                 data.get('new_values'), data.get('user_name', 'system'), data.get('created_at', datetime.now().isoformat()))
            )
            conn.commit()
            log = conn.execute("SELECT * FROM audit_log WHERE id = ?", (cursor.lastrowid,)).fetchone()
            return jsonify(row_to_dict(log)), 201
    except Exception as e:
        logger.error(f"Error creating audit log: {e}")
        return jsonify({'error': 'Failed to create audit log'}), 500


@app.route('/api/reports/budget-vs-actual')
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
def report_program_summary():
    try:
        with get_db() as conn:
            rows = conn.execute("""
                SELECT p.name, p.program_type, p.department, p.budget, p.spent,
                       p.start_date, p.end_date, p.status,
                       COUNT(DISTINCT c.id) as council_count,
                       COUNT(DISTINCT f.id) as finance_count
                FROM programs p
                LEFT JOIN council c ON c.isc_program_code = p.isc_program_code
                LEFT JOIN finances f ON f.isc_grant_id = p.isc_program_code
                GROUP BY p.id
                ORDER BY p.name
            """).fetchall()
            return jsonify([row_to_dict(r) for r in rows])
    except Exception as e:
        logger.error(f"Error generating program summary: {e}")
        return jsonify({'error': 'Failed to generate report'}), 500


@app.route('/api/reports/member-stats')
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
        conn.commit()

        if conn.execute("SELECT COUNT(*) FROM members").fetchone()[0] == 0:
            seed_data(conn)
            conn.commit()


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
    app.run(host='0.0.0.0', port=PORT, debug=DEBUG)