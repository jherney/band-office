"""
Gunicorn configuration for Band Office Management production deployment.
"""
import os
import multiprocessing

bind = f"0.0.0.0:{os.environ.get('BAND_OFFICE_PORT', '5000')}"
workers = int(os.environ.get('BAND_OFFICE_WORKERS', str(multiprocessing.cpu_count() * 2 + 1)))
worker_class = 'sync'
timeout = int(os.environ.get('BAND_OFFICE_TIMEOUT', '30'))
keepalive = int(os.environ.get('BAND_OFFICE_KEEPALIVE', '5'))

accesslog = os.environ.get('BAND_OFFICE_ACCESS_LOG', '-')
errorlog = os.environ.get('BAND_OFFICE_ERROR_LOG', '-')
loglevel = os.environ.get('BAND_OFFICE_LOG_LEVEL', 'info')

proc_name = 'band-office'
reload = False
spew = False

limit_request_line = 4096
limit_request_fields = 100
limit_request_field_size = 8190
