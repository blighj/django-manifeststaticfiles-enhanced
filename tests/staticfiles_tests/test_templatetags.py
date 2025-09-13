"""
Tests for the staticjs template tags
"""

import tempfile

from django.conf import STATICFILES_STORAGE_ALIAS, settings
from django.template import Context, Template
from django.test import TestCase, override_settings


@override_settings(
    STATIC_URL="/static/",
    STATIC_ROOT=tempfile.mkdtemp(),
    STORAGES={
        **settings.STORAGES,
        STATICFILES_STORAGE_ALIAS: {
            "BACKEND": ("django.contrib.staticfiles.storage.StaticFilesStorage"),
        },
    },
    INSTALLED_APPS=[
        "django.contrib.staticfiles",
        "django_manifeststaticfiles_enhanced",
    ],
)
class StaticJSTemplateTagTest(TestCase):
    """Test the staticjs template tags"""

    def test_include_staticjs_tag(self):
        """Test that the include_staticjs tag works correctly"""
        # Create a template that uses the include_staticjs tag
        template = Template("{% load staticjs %}" "{% include_staticjs %}")

        # Render the template
        rendered = template.render(Context({}))

        # Check that it contains the correct structure
        self.assertIn('<script src="/static/staticjs/django.js"', rendered)
        self.assertIn('id="staticjs-static-url"', rendered)
        self.assertIn('data-static-url="/static/"', rendered)
