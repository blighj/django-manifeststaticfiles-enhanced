"""
Tests for EnhancedManifestStaticFilesStorage
"""

import json
import tempfile
from unittest.mock import patch

from django.conf import STATICFILES_STORAGE_ALIAS, settings
from django.core.files.base import ContentFile
from django.test import TestCase, override_settings

from django_manifeststaticfiles_enhanced.storage import (
    EnhancedManifestStaticFilesStorage,
)


class MockStorage:
    """Mock storage for testing"""

    def __init__(self):
        self.files = {}

    def open(self, name):
        if name in self.files:
            return ContentFile(self.files[name])
        raise FileNotFoundError(f"File {name} not found")

    def exists(self, name):
        return name in self.files

    def save(self, name, content):
        self.files[name] = content.read()
        return name

    def delete(self, name):
        if name in self.files:
            del self.files[name]


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
class EnhancedManifestStaticFilesStorageTest(TestCase):
    """Test the enhanced storage functionality"""

    def setUp(self):
        self.storage = EnhancedManifestStaticFilesStorage()

    def test_keep_original_files_default(self):
        """Test that keep_original_files defaults to True"""
        self.assertTrue(self.storage.keep_original_files)

    def test_keep_original_files_false(self):
        """Test keep_original_files=False functionality"""
        with override_settings(
            STATICFILES_STORAGE_OPTIONS={"keep_original_files": False}
        ):
            storage = EnhancedManifestStaticFilesStorage(keep_original_files=False)
            self.assertFalse(storage.keep_original_files)

    def test_file_hash(self):
        """Test file hashing functionality"""
        content = ContentFile(b"test content")
        file_hash = self.storage.file_hash("test.css", content)
        self.assertEqual(len(file_hash), 12)
        self.assertIsInstance(file_hash, str)

    def test_hashed_name(self):
        """Test hashed name generation"""
        content = ContentFile(b"test content")
        hashed_name = self.storage.hashed_name("test.css", content)
        self.assertTrue(hashed_name.startswith("test."))
        self.assertTrue(hashed_name.endswith(".css"))
        # Check that there's a hash between the name and extension
        hash_part = hashed_name.replace("test.", "").replace(".css", "")
        self.assertTrue(len(hash_part) > 0, "Hash part should not be empty")
        self.assertTrue(hash_part.isalnum(), "Hash should be alphanumeric")

    def test_should_adjust_url(self):
        """Test URL adjustment logic"""
        # Should adjust relative URLs
        self.assertTrue(self.storage._should_adjust_url("./style.css"))
        self.assertTrue(self.storage._should_adjust_url("../style.css"))
        self.assertTrue(self.storage._should_adjust_url("style.css"))

        # Should not adjust absolute URLs
        self.assertFalse(
            self.storage._should_adjust_url("http://example.com/style.css")
        )
        self.assertFalse(
            self.storage._should_adjust_url("https://example.com/style.css")
        )
        self.assertFalse(self.storage._should_adjust_url("//example.com/style.css"))

        # Should not adjust data URLs
        self.assertFalse(
            self.storage._should_adjust_url("data:image/png;base64,iVBOR...")
        )

        # Should not adjust empty URLs
        self.assertFalse(self.storage._should_adjust_url(""))

    def test_clean_name(self):
        """Test path cleaning functionality"""
        self.assertEqual(
            self.storage.clean_name("path\\to\\file.css"), "path/to/file.css"
        )
        self.assertEqual(
            self.storage.clean_name("path/to/file.css"), "path/to/file.css"
        )


class ManifestIntegrationTest(TestCase):
    """Test manifest file integration"""

    def setUp(self):
        self.storage = EnhancedManifestStaticFilesStorage()

    def test_save_manifest(self):
        """Test manifest saving functionality"""
        self.storage.hashed_files = {
            "style.css": "style.abc123.css",
            "script.js": "script.def456.js",
        }

        with patch.object(self.storage.manifest_storage, "exists", return_value=False):
            with patch.object(self.storage.manifest_storage, "_save") as mock_save:
                self.storage.save_manifest()

                # Should have saved manifest
                mock_save.assert_called_once()
                args, kwargs = mock_save.call_args
                self.assertEqual(args[0], self.storage.manifest_name)

                # Check manifest content
                content_file = args[1]
                content = json.loads(content_file.read().decode())
                self.assertEqual(content["version"], self.storage.manifest_version)
                self.assertIn("paths", content)
                self.assertIn("hash", content)

    def test_load_manifest(self):
        """Test manifest loading functionality"""
        manifest_content = {
            "version": "1.1",
            "paths": {"style.css": "style.abc123.css"},
            "hash": "manifest-hash",
        }

        with patch.object(
            self.storage, "read_manifest", return_value=json.dumps(manifest_content)
        ):
            hashed_files, manifest_hash = self.storage.load_manifest()

            self.assertEqual(hashed_files, manifest_content["paths"])
            self.assertEqual(manifest_hash, manifest_content["hash"])
