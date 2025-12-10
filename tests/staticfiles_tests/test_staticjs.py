"""
Tests for the staticjs functionality in EnhancedManifestStaticFilesStorage
"""

import tempfile

from django.conf import STATICFILES_STORAGE_ALIAS, settings
from django.test import TestCase, override_settings

from django_manifeststaticfiles_enhanced.storage import (
    EnhancedManifestStaticFilesStorage,
)


@override_settings(
    STATIC_URL="/static/",
    STATIC_ROOT=tempfile.mkdtemp(),
    STORAGES={
        **settings.STORAGES,
        STATICFILES_STORAGE_ALIAS: {
            "BACKEND": (
                "django_manifeststaticfiles_enhanced.storage."
                "EnhancedManifestStaticFilesStorage"
            ),
        },
    },
)
class StaticJSTest(TestCase):
    """Test the staticjs functionality"""

    def setUp(self):
        self.storage = EnhancedManifestStaticFilesStorage()

    def test_staticjs_custom_settings(self):
        """Test that staticjs settings can be customized"""
        storage = EnhancedManifestStaticFilesStorage(
            staticjs_exclude_patterns=["*.foo"],
        )
        self.assertEqual(storage.staticjs_exclude_patterns, ["*.foo"])

    def test_staticjs_manifest_creation(self):
        """Test that staticjs creates a manifest JS file"""
        # Set up mock hashed files
        self.storage.hashed_files = {
            "image.png": "image.123abc.png",
            "data.json": "data.456def.json",
            # Add a JS file that should be excluded by default
            "script.js": "script.789ghi.js",
            "staticjs/django.js": "staticjs/django.123jkl.js",
        }

        # Create the staticjs manifest
        referenced_assets = ["image.png"]
        results = self.storage._create_staticjs_manifest(referenced_assets)

        name, hashed_name, processed = results
        self.assertEqual(name, "staticjs/django.js")
        self.assertTrue(hashed_name.startswith("staticjs/django."))
        self.assertTrue(hashed_name.endswith(".js"))
        self.assertTrue(processed)

        # Check that the staticjs file was added to hashed_files
        self.assertIn(
            self.storage.hash_key(self.storage.clean_name("staticjs/django.js")),
            self.storage.hashed_files,
        )

    def test_filtered_static_paths(self):
        """Test that staticjs correctly filters static paths"""
        # Set up mock hashed files
        self.storage.hashed_files = {
            "image.png": "image.123abc.png",
            "data.json": "data.456def.json",
            "script.js": "script.789ghi.js",
            "style.css": "style.abc123.css",
            "script.ts": "script.def456.ts",
            "staticjs/django.js": "staticjs/django.xyz789.js",
        }

        # Get filtered paths
        referenced_assets = ["image.png", "data.json"]
        filtered_dict = self.storage._get_filtered_static_paths(referenced_assets)

        # Check that JS, CSS, TS files are excluded by default
        self.assertIn("image.png", filtered_dict)
        self.assertIn("data.json", filtered_dict)
        self.assertNotIn("script.js", filtered_dict)
        self.assertNotIn("style.css", filtered_dict)
        self.assertNotIn("script.ts", filtered_dict)
        self.assertNotIn("staticjs/django.js", filtered_dict)

        # Verify values
        self.assertEqual(filtered_dict["image.png"], "image.123abc.png")
        self.assertEqual(filtered_dict["data.json"], "data.456def.json")

    def test_staticjs_content_generation(self):
        """Test the generated JS content"""
        # Set up a dictionary of static paths
        static_dict = {
            "image.png": "image.123abc.png",
            "data.json": "data.456def.json",
            "staticjs/django.js": "staticjs/django.xyz789.js",
        }

        # Generate JS content with strict mode
        self.storage.manifest_strict = True
        strict_js = self.storage._generate_staticjs_content(static_dict)

        # Check that the content contains the static dictionary and strict=true
        self.assertIn('"image.png":"image.123abc.png"', strict_js)
        self.assertIn('"data.json":"data.456def.json"', strict_js)
        self.assertIn("const strict = true", strict_js)

        # Generate JS content with non-strict mode
        self.storage.manifest_strict = False
        non_strict_js = self.storage._generate_staticjs_content(static_dict)

        # Check that the content contains strict=false
        self.assertIn("const strict = false", non_strict_js)
