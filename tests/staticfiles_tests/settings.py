"""
Django settings for testing
"""

import os.path
import tempfile
from pathlib import Path

SECRET_KEY = "django-manifeststaticfiles-enhanced-test-key"

DEBUG = True

INSTALLED_APPS = [
    "django.contrib.staticfiles",
    "django_manifeststaticfiles_enhanced",
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}

STATIC_URL = "/static/"
STATIC_ROOT = tempfile.mkdtemp()

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    }
]

TEST_ROOT = os.path.dirname(__file__)

TEST_SETTINGS = {
    "MEDIA_URL": "media/",
    "STATIC_URL": "static/",
    "MEDIA_ROOT": os.path.join(TEST_ROOT, "project", "site_media", "media"),
    "STATIC_ROOT": os.path.join(TEST_ROOT, "project", "site_media", "static"),
    "STATICFILES_DIRS": [
        os.path.join(TEST_ROOT, "project", "documents"),
        ("prefix", os.path.join(TEST_ROOT, "project", "prefixed")),
        Path(TEST_ROOT) / "project" / "pathlib",
    ],
    "STATICFILES_FINDERS": [
        "django.contrib.staticfiles.finders.FileSystemFinder",
        "django.contrib.staticfiles.finders.AppDirectoriesFinder",
        "django.contrib.staticfiles.finders.DefaultStorageFinder",
    ],
    "INSTALLED_APPS": [
        "django.contrib.staticfiles",
        "staticfiles_tests.apps.test",
        "staticfiles_tests.apps.no_label",
    ],
    # In particular, AuthenticationMiddleware can't be used because
    # contrib.auth isn't in INSTALLED_APPS.
    "MIDDLEWARE": [],
}


# Test-specific settings
USE_TZ = True
