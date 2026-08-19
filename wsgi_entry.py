"""
Production WSGI entry point for Band Office Management app.
"""
from app import app as application

app = application

if __name__ == '__main__':
    import os
    port = int(os.environ.get('BAND_OFFICE_PORT', '5000'))
    debug = os.environ.get('BAND_OFFICE_DEBUG', '0').lower() in ('1', 'true', 'yes')
    application.run(host='0.0.0.0', port=port, debug=debug)
