import pytest
import os
import sys
import tempfile
import sqlite3

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app, get_db, init_db, seed_data, RESOURCE_REGISTRY


@pytest.fixture
def client():
    db_fd, db_path = tempfile.mkstemp(suffix='.db')
    os.close(db_fd)
    os.environ['BAND_OFFICE_PORT'] = '5001'
    app.config['TESTING'] = True

    with app.test_client() as client:
        with app.app_context():
            import app as app_module
            app_module.DB_PATH = db_path
            init_db()
        yield client

    os.unlink(db_path)


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

    def test_create_audit_log(self, client):
        resp = client.post('/api/audit-log', json={
            'action': 'TEST',
            'table_name': 'test',
            'record_id': 1
        })
        assert resp.status_code == 201
