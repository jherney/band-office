import pytest
import os
import sys
import tempfile
import sqlite3

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Keep the generated secret key out of the repository's data/ directory
os.environ['BAND_OFFICE_DATA_DIR'] = tempfile.mkdtemp()

import functools
import app as app_module
from werkzeug.security import generate_password_hash
from app import app, get_db, init_db, seed_data, create_user, RESOURCE_REGISTRY

# Production hashing (scrypt) is deliberately slow; verification reads the method from the hash
app_module.generate_password_hash = functools.partial(generate_password_hash, method='pbkdf2:sha256:1000')

PASSWORD = 'correct-horse-battery'


@pytest.fixture
def db():
    import app as app_module
    db_fd, db_path = tempfile.mkstemp(suffix='.db')
    os.close(db_fd)
    app.config['TESTING'] = True
    app.config['SESSION_COOKIE_SECURE'] = False  # the test client speaks plain http
    app_module.DB_PATH = db_path
    init_db()
    yield db_path
    os.unlink(db_path)


def add_user(username, role, password=PASSWORD):
    with get_db() as conn:
        row = create_user(conn, {'username': username, 'role': role, 'password': password})
        conn.commit()
        return row['id']


def login(client, username, password=PASSWORD):
    token = client.get('/api/auth/csrf').get_json()['csrf_token']
    resp = client.post('/api/auth/login', json={'username': username, 'password': password},
                       headers={'X-CSRF-Token': token})
    if resp.status_code == 200:
        # Send the CSRF token on every request, as the frontend does for writes
        client.environ_base['HTTP_X_CSRF_TOKEN'] = resp.get_json()['csrf_token']
    return resp


@pytest.fixture
def signed_in(db):
    """Factory: a test client signed in as a new user with the given role."""
    def make(role, username=None):
        username = username or f'{role}-user'
        add_user(username, role)
        client = app.test_client()
        assert login(client, username).status_code == 200
        return client
    return make


@pytest.fixture
def client(signed_in):
    return signed_in('admin')


class TestDashboard:
    def test_dashboard_returns_stats(self, client):
        resp = client.get('/api/dashboard')
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'members' in data
        assert 'staff' in data
        assert 'housing_units' in data
        assert 'housing_by_status' in data
        assert 'finances_by_category' in data

    def test_dashboard_pending_approvals(self, client):
        resp = client.get('/api/dashboard/pending-approvals')
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'total_pending' in data
        assert 'leave_requests' in data


class TestMembers:
    def test_list_members(self, client):
        resp = client.get('/api/members')
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'members' in data
        assert 'total' in data
        assert data['total'] >= 5

    def test_get_member(self, client):
        resp = client.get('/api/members/1')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['band_number'] == 'B001-001'

    def test_get_member_not_found(self, client):
        resp = client.get('/api/members/9999')
        assert resp.status_code == 404

    def test_create_member(self, client):
        resp = client.post('/api/members', json={
            'band_number': 'TEST-001',
            'surname': 'Test',
            'given_name': 'User'
        })
        assert resp.status_code == 201
        data = resp.get_json()
        assert data['surname'] == 'Test'

    def test_create_member_missing_required(self, client):
        resp = client.post('/api/members', json={'surname': 'NoBand'})
        assert resp.status_code == 400
        data = resp.get_json()
        assert 'Missing required fields' in data['error']

    def test_update_member(self, client):
        resp = client.put('/api/members/1', json={'surname': 'Updated'})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['surname'] == 'Updated'

    def test_delete_member(self, client):
        resp = client.post('/api/members', json={
            'band_number': 'DELETE-ME',
            'surname': 'Delete',
            'given_name': 'Me'
        })
        assert resp.status_code == 201
        member_id = resp.get_json()['id']
        resp = client.delete(f'/api/members/{member_id}')
        assert resp.status_code == 200
        assert resp.get_json()['success'] is True


class TestStaff:
    def test_list_staff(self, client):
        resp = client.get('/api/staff')
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data) >= 5

    def test_create_staff(self, client):
        resp = client.post('/api/staff', json={
            'surname': 'NewStaff',
            'given_name': 'Test'
        })
        assert resp.status_code == 201


class TestDocuments:
    def test_list_documents(self, client):
        resp = client.get('/api/documents')
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data) >= 5

    def test_upload_invalid_extension(self, client):
        resp = client.post('/api/documents/upload', data={}, content_type='multipart/form-data')
        assert resp.status_code == 400


class TestNotifications:
    def test_mark_all_read(self, client):
        resp = client.post('/api/notifications/mark-all-read')
        assert resp.status_code == 200
        assert resp.get_json()['success'] is True


class TestAuditLog:
    def test_list_audit_log(self, client):
        resp = client.get('/api/audit-log')
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data) >= 4

    def test_clients_cannot_write_audit_log(self, client):
        assert client.post('/api/audit-log', json={'action': 'TEST', 'table_name': 'test'}).status_code == 405

    def _latest(self, client, table):
        return client.get(f'/api/audit-log?table={table}').get_json()[0]

    def test_crud_writes_audit_entries(self, client):
        import json
        member_id = client.post('/api/members', json={'band_number': 'AUD-1', 'surname': 'Aud', 'given_name': 'It'}).get_json()['id']
        entry = self._latest(client, 'members')
        assert (entry['action'], entry['record_id']) == ('CREATE', member_id)
        assert json.loads(entry['new_values'])['band_number'] == 'AUD-1'

        client.put(f'/api/members/{member_id}', json={'surname': 'Changed'})
        entry = self._latest(client, 'members')
        assert entry['action'] == 'UPDATE'
        assert json.loads(entry['old_values']) == {'surname': 'Aud'}
        assert json.loads(entry['new_values']) == {'surname': 'Changed'}

        client.delete(f'/api/members/{member_id}')
        entry = self._latest(client, 'members')
        assert entry['action'] == 'DELETE'
        assert json.loads(entry['old_values'])['surname'] == 'Changed'

    def test_failed_update_writes_no_audit_entry(self, client):
        before = len(client.get('/api/audit-log').get_json())
        assert client.put('/api/members/9999', json={'surname': 'x'}).status_code == 404
        assert len(client.get('/api/audit-log').get_json()) == before


class TestDocumentFileSafety:
    def test_serve_rejects_path_outside_documents(self, client):
        resp = client.post('/api/documents', json={'title': 'x', 'file_path': '/etc/hostname'})
        doc_id = resp.get_json()['id']
        assert client.get(f'/api/documents/{doc_id}/file').status_code == 404

    def test_serve_rejects_relative_traversal(self, client):
        resp = client.post('/api/documents', json={'title': 'x', 'file_path': 'documents/../app.py'})
        doc_id = resp.get_json()['id']
        assert client.get(f'/api/documents/{doc_id}/file').status_code == 404

    def test_upload_strips_path_and_round_trips(self, client):
        import io
        import app as app_module
        resp = client.post('/api/documents/upload', content_type='multipart/form-data',
                           data={'file': (io.BytesIO(b'hello'), '../../evil.txt')})
        assert resp.status_code == 200
        file_path = resp.get_json()['file_path']
        saved = os.path.join(app_module.BASE_DIR, file_path)
        try:
            assert os.path.dirname(os.path.realpath(saved)) == app_module.DOCUMENTS_DIR
            assert file_path.endswith('_evil.txt')
            doc_id = client.post('/api/documents', json={'title': 'up', 'file_path': file_path}).get_json()['id']
            resp = client.get(f'/api/documents/{doc_id}/file')
            assert resp.status_code == 200
            assert resp.data == b'hello'
            resp.close()
        finally:
            os.remove(saved)

    @pytest.mark.parametrize('name,inline', [('t_page.html', False), ('t_note.txt', True)])
    def test_only_allowed_types_render_inline(self, client, name, inline):
        import app as app_module
        os.makedirs(app_module.DOCUMENTS_DIR, exist_ok=True)
        path = os.path.join(app_module.DOCUMENTS_DIR, name)
        with open(path, 'w') as f:
            f.write('<script>alert(1)</script>')
        try:
            doc_id = client.post('/api/documents', json={'title': 'x', 'file_path': f'documents/{name}'}).get_json()['id']
            resp = client.get(f'/api/documents/{doc_id}/file')
            assert resp.status_code == 200
            assert resp.headers.get('Content-Disposition', '').startswith('attachment') is not inline
            assert resp.headers['X-Content-Type-Options'] == 'nosniff'
            resp.close()
        finally:
            os.remove(path)

    def test_upload_keeps_extension_for_non_ascii_name(self, client):
        import io
        import app as app_module
        resp = client.post('/api/documents/upload', content_type='multipart/form-data',
                           data={'file': (io.BytesIO(b'%PDF'), 'отчёт.pdf')})
        assert resp.status_code == 200
        file_path = resp.get_json()['file_path']
        os.remove(os.path.join(app_module.BASE_DIR, file_path))
        assert file_path.endswith('.pdf')


class TestUpdateAllResources:
    @pytest.mark.parametrize('url,field,value', [
        ('finances', 'amount', 1),
        ('notifications', 'is_read', 1),
        ('leave-requests', 'status', 'approved'),
        ('purchase-requisitions', 'status', 'approved'),
        ('incident-reports', 'status', 'resolved'),
        ('meeting-requests', 'status', 'approved'),
        ('tasks', 'status', 'completed'),
    ])
    def test_update(self, client, url, field, value):
        resp = client.put(f'/api/{url}/1', json={field: value})
        assert resp.status_code == 200
        assert resp.get_json()[field] == value


class TestReports:
    def test_program_summary(self, client):
        resp = client.get('/api/reports/program-summary')
        assert resp.status_code == 200
        bsf = next(r for r in resp.get_json() if r['name'] == 'Band Support Funding')
        assert bsf['council_count'] == 1

    def test_program_summary_requires_exact_reference(self, client):
        client.post('/api/council', json={'item_type': 'resolution', 'title': 'x', 'date': '2025-01-01',
                                          'isc_reference': 'isc-xbsf-2025-001'})
        bsf = next(r for r in client.get('/api/reports/program-summary').get_json()
                   if r['name'] == 'Band Support Funding')
        assert bsf['council_count'] == 1


class TestBadInput:
    def test_create_without_json_body(self, client):
        assert client.post('/api/staff').status_code == 400

    def test_update_with_non_object_body(self, client):
        assert client.put('/api/staff/1', json=[1, 2]).status_code == 400

    @pytest.mark.parametrize('value', [{'a': 1}, [1, 2]])
    def test_nested_values_rejected(self, client, value):
        assert client.put('/api/staff/1', json={'surname': value}).status_code == 400
        assert client.post('/api/staff', json={'surname': 'A', 'given_name': value}).status_code == 400

    @pytest.mark.parametrize('qs', ['page=abc', 'per_page=x', 'page=0', 'per_page=501',
                                    'page=99999999999999999999'])
    def test_bad_paging(self, client, qs):
        assert client.get(f'/api/members?{qs}').status_code == 400

    def test_paging_ignored_on_unpaginated_resource(self, client):
        resp = client.get('/api/tasks?page=0&per_page=all')
        assert resp.status_code == 200
        assert len(resp.get_json()) >= 6


class TestFilters:
    def test_filter_uses_resource_field(self, client):
        data = client.get('/api/documents?document_type=report').get_json()
        assert data and all(d['document_type'] == 'report' for d in data)

    def test_other_params_do_not_filter(self, client):
        total = len(client.get('/api/documents').get_json())
        assert len(client.get('/api/documents?status=report').get_json()) == total
        assert len(client.get('/api/documents?type=report').get_json()) == total

    @pytest.mark.parametrize('url,field,value', [
        ('housing', 'status', 'vacant'),
        ('programs', 'status', 'active'),
        ('council', 'item_type', 'meeting'),
        ('finances', 'type', 'income'),
    ])
    def test_filters_used_by_frontend(self, client, url, field, value):
        expected = [r for r in client.get(f'/api/{url}').get_json() if r[field] == value]
        assert expected and client.get(f'/api/{url}?{field}={value}').get_json() == expected


class TestDocumentFileCleanup:
    def _upload(self, client, name='a.txt'):
        import io
        resp = client.post('/api/documents/upload', content_type='multipart/form-data',
                           data={'file': (io.BytesIO(b'x'), name)})
        return resp.get_json()['file_path']

    def _abs(self, file_path):
        import app as app_module
        return os.path.join(app_module.BASE_DIR, file_path)

    def test_delete_removes_file(self, client):
        file_path = self._upload(client)
        doc_id = client.post('/api/documents', json={'title': 'x', 'file_path': file_path}).get_json()['id']
        assert client.delete(f'/api/documents/{doc_id}').status_code == 200
        assert not os.path.exists(self._abs(file_path))

    def test_delete_keeps_file_still_referenced(self, client):
        file_path = self._upload(client)
        first = client.post('/api/documents', json={'title': 'a', 'file_path': file_path}).get_json()['id']
        second = client.post('/api/documents', json={'title': 'b', 'file_path': file_path}).get_json()['id']
        client.delete(f'/api/documents/{first}')
        assert os.path.exists(self._abs(file_path))
        client.delete(f'/api/documents/{second}')
        assert not os.path.exists(self._abs(file_path))

    def test_delete_with_missing_file_succeeds(self, client):
        doc_id = client.post('/api/documents', json={'title': 'x', 'file_path': 'documents/gone.txt'}).get_json()['id']
        assert client.delete(f'/api/documents/{doc_id}').status_code == 200

    def test_delete_never_touches_files_outside_documents(self, client, tmp_path):
        outside = tmp_path / 'keep.txt'
        outside.write_text('keep')
        doc_id = client.post('/api/documents', json={'title': 'x', 'file_path': str(outside)}).get_json()['id']
        assert client.delete(f'/api/documents/{doc_id}').status_code == 200
        assert outside.exists()

    def test_replacing_file_removes_old_one(self, client):
        old_path = self._upload(client)
        new_path = self._upload(client)
        doc_id = client.post('/api/documents', json={'title': 'x', 'file_path': old_path}).get_json()['id']
        client.put(f'/api/documents/{doc_id}', json={'file_path': new_path})
        assert not os.path.exists(self._abs(old_path))
        client.delete(f'/api/documents/{doc_id}')
        assert not os.path.exists(self._abs(new_path))


def _run_with_env(code, **env):
    import subprocess
    base = {k: v for k, v in os.environ.items() if not k.startswith('BAND_OFFICE_')}
    env.setdefault('BAND_OFFICE_DATA_DIR', tempfile.mkdtemp())
    result = subprocess.run([sys.executable, '-c', code], env={**base, **env}, capture_output=True, text=True,
                            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


CORS_PROBE = """
from app import app
r = app.test_client().get('/health', headers={'Origin': 'http://evil.example'})
r2 = app.test_client().get('/health', headers={'Origin': 'https://office.example'})
print(r.headers.get('Access-Control-Allow-Origin'), r2.headers.get('Access-Control-Allow-Origin'))
"""


class TestDeploymentDefaults:
    def test_no_cors_headers_by_default(self):
        assert _run_with_env(CORS_PROBE) == 'None None'

    def test_cors_only_for_configured_origin(self):
        assert _run_with_env(CORS_PROBE, BAND_OFFICE_CORS_ORIGIN='https://office.example') == 'None https://office.example'

    def test_gunicorn_binds_loopback_by_default(self):
        assert _run_with_env('import gunicorn_config; print(gunicorn_config.bind)') == '127.0.0.1:5000'
        assert _run_with_env('import gunicorn_config; print(gunicorn_config.bind)', BAND_OFFICE_HOST='0.0.0.0') == '0.0.0.0:5000'

    def test_generated_secret_key_is_persisted_and_shared(self, tmp_path):
        code = 'from app import app; print(app.secret_key)'
        first = _run_with_env(code, BAND_OFFICE_DATA_DIR=str(tmp_path))
        assert len(first) == 64
        # A second process (another worker, or after a restart) gets the same key
        assert _run_with_env(code, BAND_OFFICE_DATA_DIR=str(tmp_path)) == first
        assert oct((tmp_path / 'secret_key').stat().st_mode & 0o777) == '0o600'
        assert _run_with_env(code, BAND_OFFICE_DATA_DIR=str(tmp_path),
                             BAND_OFFICE_SECRET_KEY='change-this-in-production') == first
        assert _run_with_env(code, BAND_OFFICE_SECRET_KEY='real-key') == 'real-key'


class TestAuthentication:
    def test_api_requires_login(self, db):
        client = app.test_client()
        for url in ['/api/dashboard', '/api/members', '/api/tasks', '/api/users', '/api/auth/me', '/api/export/members.csv']:
            assert client.get(url).status_code == 401, url
        assert client.post('/api/tasks', json={'title': 'x'}).status_code == 401

    def test_page_and_health_are_public(self, db):
        client = app.test_client()
        assert client.get('/').status_code == 200
        assert client.get('/health').status_code == 200

    def test_wrong_password_and_unknown_user_look_the_same(self, db):
        add_user('alice', 'staff')
        client = app.test_client()
        wrong = login(client, 'alice', 'not-the-password')
        unknown = login(client, 'nobody', 'not-the-password')
        assert wrong.status_code == unknown.status_code == 401
        assert wrong.get_json() == unknown.get_json()

    def test_lockout_after_repeated_failures(self, db):
        add_user('alice', 'staff')
        client = app.test_client()
        for _ in range(5):
            wrong = login(client, 'alice', 'wrong-password!')
            assert wrong.status_code == 401
        blocked = login(client, 'alice')
        # Same answer as a wrong password, so a lockout doesn't reveal that the account exists
        assert (blocked.status_code, blocked.get_json()) == (401, wrong.get_json())

    def test_successful_login_resets_failure_count(self, db):
        add_user('alice', 'staff')
        client = app.test_client()
        for _ in range(4):
            login(client, 'alice', 'wrong-password!')
        assert login(client, 'alice').status_code == 200
        for _ in range(4):
            login(client, 'alice', 'wrong-password!')
        assert login(client, 'alice').status_code == 200

    def test_writes_require_csrf_token(self, signed_in):
        client = signed_in('staff')
        token = client.environ_base.pop('HTTP_X_CSRF_TOKEN')
        assert client.post('/api/tasks', json={'title': 'x'}).status_code == 403
        assert client.post('/api/tasks', json={'title': 'x'}, headers={'X-CSRF-Token': 'wrong'}).status_code == 403
        assert client.get('/api/tasks').status_code == 200
        assert client.post('/api/tasks', json={'title': 'x'}, headers={'X-CSRF-Token': token}).status_code == 201

    def test_logout_ends_session(self, signed_in):
        client = signed_in('staff')
        assert client.post('/api/auth/logout').status_code == 200
        assert client.get('/api/tasks').status_code == 401

    def test_me_lists_sections_for_role(self, signed_in):
        sections = signed_in('registry').get('/api/auth/me').get_json()['sections']
        assert 'members' in sections and 'reports' in sections
        assert not {'finances', 'staff', 'audit', 'users'} & set(sections)

    def test_audit_entries_name_the_user(self, signed_in):
        client = signed_in('staff', 'carol')
        client.post('/api/tasks', json={'title': 'x'})
        assert client.get('/api/audit-log').status_code == 403
        admin = signed_in('admin', 'root')
        assert admin.get('/api/audit-log?table=tasks').get_json()[0]['user_name'] == 'carol'


class TestRoles:
    @pytest.mark.parametrize('role,url,status', [
        ('staff', '/api/tasks', 200),
        ('staff', '/api/housing', 200),
        ('staff', '/api/members', 403),
        ('staff', '/api/members/1', 403),
        ('staff', '/api/staff', 403),
        ('staff', '/api/finances', 403),
        ('staff', '/api/export/finances.csv', 403),
        ('staff', '/api/export/members.csv', 403),
        ('staff', '/api/reports/budget-vs-actual', 403),
        ('staff', '/api/reports/member-stats', 403),
        ('staff', '/api/audit-log', 403),
        ('staff', '/api/users', 403),
        ('registry', '/api/members', 200),
        ('registry', '/api/export/members.csv', 200),
        ('registry', '/api/reports/member-stats', 200),
        ('registry', '/api/finances', 403),
        ('finance', '/api/finances', 200),
        ('finance', '/api/reports/program-summary', 200),
        ('finance', '/api/members', 403),
        ('finance', '/api/staff', 403),
        ('admin', '/api/staff', 200),
        ('admin', '/api/users', 200),
    ])
    def test_read_access(self, signed_in, role, url, status):
        assert signed_in(role).get(url).status_code == status

    def test_role_also_guards_writes(self, signed_in):
        client = signed_in('staff')
        assert client.put('/api/members/1', json={'surname': 'x'}).status_code == 403
        assert client.delete('/api/finances/1').status_code == 403
        assert client.post('/api/users', json={'username': 'eve', 'role': 'admin', 'password': PASSWORD}).status_code == 403

    @pytest.mark.parametrize('role,visible,hidden', [
        ('staff', set(), {'members', 'staff', 'staff_by_department', 'finances_by_category', 'recent_finances'}),
        ('finance', {'finances_by_category', 'recent_finances'}, {'members', 'staff', 'staff_by_department'}),
        ('registry', {'members'}, {'staff', 'staff_by_department', 'finances_by_category', 'recent_finances'}),
        ('admin', {'members', 'staff', 'staff_by_department', 'finances_by_category', 'recent_finances'}, set()),
    ])
    def test_dashboard_widgets_follow_section_roles(self, signed_in, role, visible, hidden):
        data = signed_in(role).get('/api/dashboard').get_json()
        assert visible <= data.keys() and not hidden & data.keys()
        assert {'housing_units', 'tasks_by_status', 'recent_resolutions'} <= data.keys()


class TestUserManagement:
    def test_create_and_list_without_password_hash(self, client):
        resp = client.post('/api/users', json={'username': 'dave', 'role': 'finance', 'password': PASSWORD})
        assert resp.status_code == 201
        users = client.get('/api/users').get_json()
        assert 'dave' in [u['username'] for u in users]
        assert all('password_hash' not in u for u in users)
        assert login(app.test_client(), 'dave').status_code == 200

    @pytest.mark.parametrize('body', [
        {'username': 'x', 'role': 'staff', 'password': PASSWORD},
        {'username': 'bad name', 'role': 'staff', 'password': PASSWORD},
        {'username': 'dave', 'role': 'superuser', 'password': PASSWORD},
        {'username': 'dave', 'role': 'staff', 'password': 'short'},
        {'username': 'admin-user', 'role': 'staff', 'password': PASSWORD},
    ])
    def test_invalid_user_rejected(self, client, body):
        assert client.post('/api/users', json=body).status_code == 400

    def test_deactivating_user_ends_their_session(self, signed_in, client):
        user_client = signed_in('staff', 'erin')
        erin = next(u for u in client.get('/api/users').get_json() if u['username'] == 'erin')
        assert client.put(f"/api/users/{erin['id']}", json={'is_active': '0'}).status_code == 200
        assert user_client.get('/api/tasks').status_code == 401
        assert login(app.test_client(), 'erin').status_code == 401

    def test_role_change_ends_session_but_unchanged_edit_does_not(self, signed_in, client):
        user_client = signed_in('staff', 'erin')
        erin = next(u for u in client.get('/api/users').get_json() if u['username'] == 'erin')
        client.put(f"/api/users/{erin['id']}", json={'username': 'erin', 'role': 'staff', 'is_active': '1', 'password': ''})
        assert user_client.get('/api/tasks').status_code == 200
        client.put(f"/api/users/{erin['id']}", json={'role': 'finance'})
        assert user_client.get('/api/tasks').status_code == 401

    def test_admin_password_reset(self, signed_in, client):
        signed_in('staff', 'erin')
        erin = next(u for u in client.get('/api/users').get_json() if u['username'] == 'erin')
        client.put(f"/api/users/{erin['id']}", json={'password': 'a-brand-new-password'})
        assert login(app.test_client(), 'erin').status_code == 401
        assert login(app.test_client(), 'erin', 'a-brand-new-password').status_code == 200

    def test_cannot_remove_last_active_admin(self, client):
        me = client.get('/api/auth/me').get_json()
        assert client.put(f"/api/users/{me['id']}", json={'is_active': '0'}).status_code == 400
        assert client.put(f"/api/users/{me['id']}", json={'role': 'staff'}).status_code == 400
        assert client.get('/api/users').status_code == 200

    def test_users_cannot_be_deleted(self, client):
        me = client.get('/api/auth/me').get_json()
        assert client.delete(f"/api/users/{me['id']}").status_code == 405

    def test_change_own_password(self, signed_in):
        user_client = signed_in('staff', 'erin')
        other_session = app.test_client()
        login(other_session, 'erin')
        assert user_client.post('/api/auth/password', json={'current_password': 'wrong', 'new_password': 'another-long-password'}).status_code == 400
        assert user_client.post('/api/auth/password', json={'current_password': PASSWORD, 'new_password': 'short'}).status_code == 400
        assert user_client.post('/api/auth/password', json={'current_password': PASSWORD, 'new_password': 'another-long-password'}).status_code == 200
        assert user_client.get('/api/tasks').status_code == 200  # this session continues
        assert other_session.get('/api/tasks').status_code == 401  # others end
        assert login(app.test_client(), 'erin', 'another-long-password').status_code == 200

    def test_cli_creates_user(self, db):
        result = app.test_cli_runner().invoke(args=['create-user', 'frank', '--role', 'registry'],
                                              input=f'{PASSWORD}\n{PASSWORD}\n')
        assert result.exit_code == 0, result.output
        assert login(app.test_client(), 'frank').get_json()['role'] == 'registry'
        result = app.test_cli_runner().invoke(args=['create-user', 'frank'], input=f'{PASSWORD}\n{PASSWORD}\n')
        assert result.exit_code != 0 and 'already exists' in result.output


class TestNotificationsPage:
    def test_list_is_plain_array_with_read_flags(self, client):
        data = client.get('/api/notifications').get_json()
        assert isinstance(data, list) and all('is_read' in n for n in data)


class TestOrphanUploadSweep:
    def test_old_unattached_uploads_are_swept(self, client):
        import io, time
        import app as app_module
        upload = lambda: client.post('/api/documents/upload', content_type='multipart/form-data',
                                     data={'file': (io.BytesIO(b'x'), 'a.txt')}).get_json()['file_path']
        orphan, attached = upload(), upload()
        client.post('/api/documents', json={'title': 'kept', 'file_path': attached})
        manual = os.path.join(app_module.DOCUMENTS_DIR, 'manual-note.txt')
        open(manual, 'w').close()
        old = time.time() - app_module.ORPHAN_UPLOAD_MAX_AGE - 60
        paths = [os.path.join(app_module.BASE_DIR, p) for p in (orphan, attached)] + [manual]
        for p in paths:
            os.utime(p, (old, old))
        fresh = upload()
        try:
            assert not os.path.exists(paths[0])  # old and unattached: swept
            assert os.path.exists(paths[1])      # attached to a document: kept
            assert os.path.exists(manual)        # not an app upload: kept
            assert os.path.exists(os.path.join(app_module.BASE_DIR, fresh))  # recent: kept
        finally:
            for p in paths[1:] + [os.path.join(app_module.BASE_DIR, fresh)]:
                if os.path.exists(p):
                    os.remove(p)


class TestBlankFields:
    def test_blank_and_missing_fields_get_column_defaults(self, client):
        for body in ({'title': 'modal', 'status': '', 'priority': '  ', 'due_date': ''}, {'title': 'api'}):
            task = client.post('/api/tasks', json=body).get_json()
            assert (task['status'], task['priority'], task['due_date']) == ('pending', 'medium', None)

    def test_zero_is_a_real_value_for_required_fields(self, client):
        resp = client.post('/api/finances', json={'category': 'c', 'type': 'income', 'amount': 0, 'date': '2025-01-01'})
        assert resp.status_code == 201 and resp.get_json()['amount'] == 0

    def test_blank_required_field_rejected(self, client):
        assert client.post('/api/tasks', json={'title': '   '}).status_code == 400
        assert client.put('/api/tasks/1', json={'title': ''}).status_code == 400
        assert client.get('/api/tasks/1').get_json()['title'] == 'Submit ISC Quarterly Report'

    def test_blank_optional_field_clears_to_null(self, client):
        import json
        resp = client.put('/api/tasks/1', json={'title': 'Kept', 'assigned_to': '', 'due_date': None})
        assert resp.status_code == 200
        assert (resp.get_json()['assigned_to'], resp.get_json()['due_date']) == (None, None)
        entry = client.get('/api/audit-log?table=tasks').get_json()[0]
        assert json.loads(entry['new_values']) == {'title': 'Kept', 'assigned_to': None, 'due_date': None}


class TestSignInHardening:
    def test_sign_in_requires_pre_session_token(self, db):
        add_user('alice', 'staff')
        body = {'username': 'alice', 'password': PASSWORD}
        assert app.test_client().post('/api/auth/login', json=body).status_code == 403
        other = app.test_client().get('/api/auth/csrf').get_json()['csrf_token']
        assert app.test_client().post('/api/auth/login', json=body, headers={'X-CSRF-Token': other}).status_code == 403
        assert login(app.test_client(), 'alice').status_code == 200

    def test_sign_in_issues_a_new_token(self, db):
        add_user('alice', 'staff')
        client = app.test_client()
        before = client.get('/api/auth/csrf').get_json()['csrf_token']
        after = client.post('/api/auth/login', json={'username': 'alice', 'password': PASSWORD},
                            headers={'X-CSRF-Token': before}).get_json()['csrf_token']
        assert after != before


class TestSignInAudit:
    def _entries(self, admin):
        return [(a['action'], a['user_name'], a['new_values']) for a in admin.get('/api/audit-log?table=users').get_json()][::-1]

    def test_sign_in_and_out_are_audited(self, signed_in):
        user = signed_in('staff', 'erin')
        user.post('/api/auth/logout')
        admin = signed_in('admin', 'root')
        actions = [(a, u) for a, u, _ in self._entries(admin)]
        assert ('LOGIN', 'erin') in actions and ('LOGOUT', 'erin') in actions and ('LOGIN', 'root') in actions

    def test_failures_and_lockout_are_audited(self, signed_in):
        import json
        add_user('alice', 'staff')
        client = app.test_client()
        for _ in range(5):
            login(client, 'alice', 'wrong-password!')
        login(client, 'alice')  # rejected while locked: not logged again
        admin = signed_in('admin', 'root')
        alice = [(a, json.loads(v)) for a, u, v in self._entries(admin) if u == 'alice' and a != 'CREATE']
        assert [a for a, _ in alice] == ['LOGIN_FAILED'] * 5 + ['LOCKOUT']
        assert alice[0][1]['reason'] == 'wrong password'

    def test_unknown_usernames_are_not_stored_and_are_throttled(self, signed_in):
        import json
        client = app.test_client()
        for _ in range(3):
            login(client, 'my-secret-password-typed-here', 'x')
        admin = signed_in('admin', 'root')
        unknown = [(u, json.loads(v)) for a, u, v in self._entries(admin) if a == 'LOGIN_FAILED']
        assert len(unknown) == 1
        assert unknown[0][0] == '(unknown)' and unknown[0][1]['reason'] == 'unknown username'
        assert 'my-secret' not in json.dumps(admin.get('/api/audit-log').get_json())

    def test_inactive_account_failure_reason(self, signed_in, client):
        add_user('gone', 'staff')
        gone = next(u for u in client.get('/api/users').get_json() if u['username'] == 'gone')
        client.put(f"/api/users/{gone['id']}", json={'is_active': '0'})
        assert login(app.test_client(), 'gone').status_code == 401
        import json
        entry = client.get('/api/audit-log?table=users').get_json()[0]
        assert (entry['action'], json.loads(entry['new_values'])['reason']) == ('LOGIN_FAILED', 'inactive account')


class TestReviewFixes:
    def test_add_user_with_active_left_blank(self, client):
        resp = client.post('/api/users', json={'username': 'newbie', 'role': 'staff', 'password': PASSWORD, 'is_active': ''})
        assert resp.status_code == 201 and resp.get_json()['is_active'] == 1
        assert client.put(f"/api/users/{resp.get_json()['id']}", json={'role': 'staff', 'is_active': ''}).status_code == 200

    def test_leave_requests_status_filter(self, client):
        expected = [r for r in client.get('/api/leave-requests').get_json() if r['status'] == 'pending']
        assert expected and client.get('/api/leave-requests?status=pending').get_json() == expected

    def test_unexpected_errors_in_api_return_json(self, client, monkeypatch):
        import app as app_module
        def boom(row):
            raise RuntimeError('boom')
        monkeypatch.setattr(app_module, 'public_user', boom)
        monkeypatch.setitem(app.config, 'PROPAGATE_EXCEPTIONS', False)
        resp = client.get('/api/users')
        assert resp.status_code == 500 and resp.get_json() == {'error': 'Internal server error'}

    def test_audit_timestamps_are_utc_and_old_rows_are_normalised(self, client):
        import re
        client.post('/api/tasks', json={'title': 'x'})
        newest = client.get('/api/audit-log').get_json()[0]['created_at']
        assert re.fullmatch(r'\d{4}-\d\d-\d\d \d\d:\d\d:\d\d', newest)
        with get_db() as conn:
            conn.execute("INSERT INTO audit_log (action, table_name, created_at) VALUES ('OLD', 't', '2026-01-02T03:04:05.123456')")
            conn.commit()
        init_db()
        with get_db() as conn:
            fixed = conn.execute("SELECT created_at FROM audit_log WHERE action = 'OLD'").fetchone()[0]
        assert re.fullmatch(r'2026-01-0[12] \d\d:04:05', fixed)

    def test_audit_entries_in_the_same_second_keep_their_order(self, client):
        for title in ('a', 'b', 'c'):
            client.post('/api/tasks', json={'title': title})
        latest = client.get('/api/audit-log?table=tasks').get_json()[:3]
        assert [e['record_id'] for e in latest] == sorted([e['record_id'] for e in latest], reverse=True)

    def test_dropped_secure_cookie_is_explained(self, db, monkeypatch):
        add_user('alice', 'staff')
        monkeypatch.setitem(app.config, 'SESSION_COOKIE_SECURE', True)
        body = {'username': 'alice', 'password': PASSWORD}
        # The browser threw the cookie away, so the session holds no token
        resp = app.test_client().post('/api/auth/login', json=body, headers={'X-CSRF-Token': 'from-csrf-call'})
        assert resp.status_code == 403 and 'HTTPS' in resp.get_json()['error']
        https = app.test_client().post('/api/auth/login', json=body, base_url='https://localhost',
                                      headers={'X-CSRF-Token': 'x'})
        assert https.get_json()['error'] == 'Missing or invalid CSRF token'

    def test_sweep_reads_document_references_once(self, client, monkeypatch):
        import io, time
        import app as app_module
        names = [f'{uuid_hex}_old.txt' for uuid_hex in ('a' * 32, 'b' * 32, 'c' * 32)]
        old = time.time() - app_module.ORPHAN_UPLOAD_MAX_AGE - 60
        for name in names:
            path = os.path.join(app_module.DOCUMENTS_DIR, name)
            open(path, 'w').close()
            os.utime(path, (old, old))
        calls = []
        real = app_module.referenced_document_paths
        monkeypatch.setattr(app_module, 'referenced_document_paths', lambda conn: calls.append(1) or real(conn))
        fresh = client.post('/api/documents/upload', content_type='multipart/form-data',
                            data={'file': (io.BytesIO(b'x'), 'n.txt')}).get_json()['file_path']
        os.remove(os.path.join(app_module.BASE_DIR, fresh))
        assert len(calls) == 1
        assert not any(os.path.exists(os.path.join(app_module.DOCUMENTS_DIR, n)) for n in names)


class TestSignInLimits:
    def _client(self, ip):
        client = app.test_client()
        client.environ_base['REMOTE_ADDR'] = ip
        return client

    def test_failures_elsewhere_do_not_lock_out_the_owner(self, db):
        add_user('admin1', 'admin')
        attacker = self._client('203.0.113.9')
        for _ in range(10):
            login(attacker, 'admin1', 'guessing-password')
        assert login(attacker, 'admin1').status_code == 401
        assert login(self._client('198.51.100.7'), 'admin1').status_code == 200

    def test_one_ip_trying_many_usernames_is_blocked(self, db):
        add_user('alice', 'staff')
        sprayer = self._client('203.0.113.9')
        for i in range(20):
            login(sprayer, f'user{i}', 'Spring2026!')
        assert login(sprayer, 'alice').status_code == 401
        assert login(self._client('198.51.100.7'), 'alice').status_code == 200

    def test_success_clears_that_accounts_failures(self, db):
        add_user('alice', 'staff')
        client = self._client('198.51.100.7')
        for _ in range(4):
            login(client, 'alice', 'wrong-password!')
        assert login(client, 'alice').status_code == 200
        for _ in range(4):
            login(client, 'alice', 'wrong-password!')
        assert login(client, 'alice').status_code == 200

    def test_attempted_usernames_are_not_stored(self, db):
        login(self._client('203.0.113.9'), 'hunter2-typed-as-name', 'x')
        with get_db() as conn:
            rows = conn.execute("SELECT ip, username_hash FROM login_failures").fetchall()
        assert len(rows) == 1 and 'hunter2' not in rows[0][1]

    def test_unknown_user_lockout_is_audited_once(self, signed_in):
        sprayer = self._client('203.0.113.9')
        for i in range(25):
            login(sprayer, 'ghost', 'x')
        admin = signed_in('admin', 'root')
        lockouts = [e for e in admin.get('/api/audit-log?table=users').get_json() if e['action'] == 'LOCKOUT']
        assert len(lockouts) == 1 and lockouts[0]['user_name'] == '(unknown)'


PROXY_PROBE = """
import os, app as m
m.init_db()
c = m.app.test_client()
for ip in ('198.51.100.1', '198.51.100.2'):
    t = c.get('/api/auth/csrf').get_json()['csrf_token']
    c.post('/api/auth/login', json={'username': 'x', 'password': 'y'},
           headers={'X-CSRF-Token': t, 'X-Forwarded-For': ip}, base_url='https://localhost')
with m.get_db() as conn:
    print(sorted({r[0] for r in conn.execute('SELECT ip FROM login_failures')}))
"""


def test_proxy_count_uses_forwarded_client_ip():
    assert _run_with_env(PROXY_PROBE) == "['127.0.0.1']"
    assert _run_with_env(PROXY_PROBE, BAND_OFFICE_PROXY_COUNT='1') == "['198.51.100.1', '198.51.100.2']"
