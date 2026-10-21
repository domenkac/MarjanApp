"""Production browser settings; fail before importing development defaults."""

import os

from app.server_config import configure_environment, load_configuration

_configuration = load_configuration(os.environ.get("MARJAN_SERVER_CONFIG"))
configure_environment(_configuration)

from app.settings import *

# Keep the stable file-derived key out of the process/child environment.
SECRET_KEY = _configuration.secret
DEBUG = False
ALLOWED_HOSTS = ["127.0.0.1"]
CSRF_TRUSTED_ORIGINS = []
CSRF_COOKIE_SAMESITE = "Strict"
CSRF_COOKIE_HTTPONLY = True
CSRF_FAILURE_VIEW = "app.local_gateway.csrf_failure"
LOGGING_CONFIG = None
LOGGING = {}
STATIC_URL = "/static/"
TEMPLATES = [{**TEMPLATES[0], "OPTIONS": {"context_processors": ["app.local_gateway.browser_context"]}}]
