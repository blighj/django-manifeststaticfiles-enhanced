"""
Tests for EnhancedManifestStaticFilesStorage
"""

import json
import tempfile
import unittest
from unittest.mock import MagicMock, patch

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


class CSSProcessingTest(TestCase):
    """Test CSS processing with lexer improvements"""

    def setUp(self):
        self.storage = EnhancedManifestStaticFilesStorage()
        self.hashed_files = {}

    @patch("django_manifeststaticfiles_enhanced.storage.extract_css_urls")
    def test_process_css_with_lexer(self, mock_extract_css_urls):
        """Test CSS processing with lexer when available"""
        css_content = "body { background: url('image.png'); }"
        mock_extract_css_urls.return_value = [("image.png", 23)]

        with patch.object(self.storage, "_should_adjust_url", return_value=True):
            with patch.object(
                self.storage, "_adjust_url", return_value="image.ab12.png"
            ):
                result = self.storage._process_css_urls(
                    "style.css", css_content, self.hashed_files
                )

                self.assertIn("image.ab12.png", result)
                mock_extract_css_urls.assert_called_once_with(css_content)


class JSModuleProcessingTest(TestCase):
    """Test JavaScript module processing"""

    def setUp(self):
        self.storage = EnhancedManifestStaticFilesStorage()
        self.storage.support_js_module_import_aggregation = True
        self.hashed_files = {}

    @patch("django_manifeststaticfiles_enhanced.storage.find_import_export_strings")
    def test_process_js_modules(self, mock_find_import_export_strings):
        """Test JS module processing with JsLex"""
        js_content = 'import { Component } from "./component.js";'
        mock_find_import_export_strings.return_value = [("./component.js", 25)]

        with patch.object(self.storage, "_should_adjust_url", return_value=True):
            with patch.object(
                self.storage, "_adjust_url", return_value="./hashed-component.js"
            ):
                result = self.storage._process_js_modules(
                    "app.js", js_content, self.hashed_files
                )

                self.assertIn("hashed-component.js", result)
                mock_find_import_export_strings.assert_called_once_with(js_content)

    def test_process_js_modules_no_imports(self):
        """Test JS module processing when no imports are found"""
        js_content = "const x = 42; console.log(x);"

        result = self.storage._process_js_modules(
            "app.js", js_content, self.hashed_files
        )

        # Should return original content unchanged when no imports found
        self.assertEqual(result, js_content)


class PostProcessOptimizationTest(TestCase):
    """Test post-processing optimizations from ticket_28200"""

    def setUp(self):
        self.storage = EnhancedManifestStaticFilesStorage()

    def test_optimization_unchanged_files(self):
        """Test that unchanged files don't get reprocessed unnecessarily"""
        # Mock the storage methods
        with patch.object(self.storage, "exists", return_value=True):
            with patch.object(self.storage, "_save"):
                with patch.object(self.storage, "delete"):
                    with patch.object(
                        self.storage, "hashed_name", return_value="same-hash.css"
                    ):

                        # Simulate processing where hash hasn't changed
                        paths = {"style.css": (self.storage, "style.css")}
                        adjustable_paths = ["style.css"]
                        hashed_files = {}

                        # Mock file content that doesn't trigger CSS processing
                        mock_file = MagicMock()
                        mock_file.read.return_value = b"body { color: red; }"

                        with patch.object(self.storage, "open", return_value=mock_file):
                            with patch.object(
                                self.storage,
                                "_process_css_urls",
                                return_value="body { color: red; }",
                            ):
                                with patch.object(
                                    self.storage,
                                    "_process_sourcemapping_regexs",
                                    return_value="body { color: red; }",
                                ):
                                    results = list(
                                        self.storage._post_process(
                                            paths, adjustable_paths, hashed_files
                                        )
                                    )

                            # Should not have called _save since
                            #  file exists and hash is same
                            self.assertEqual(len(results), 1)
                            name, hashed_name, processed, substitutions = results[0]
                            self.assertEqual(name, "style.css")


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


class KeepOriginalFilesTest(TestCase):
    """Test keep_original_files functionality from ticket_27929"""

    def test_keep_original_files_true(self):
        """Test that original files are kept when keep_original_files=True"""
        storage = EnhancedManifestStaticFilesStorage(keep_original_files=True)

        with patch.object(storage, "exists", return_value=True):
            with patch.object(storage, "delete") as mock_delete:
                # Simulate post_process
                paths = {"style.css": (storage, "style.css")}

                # Mock the parent post_process to yield processed files
                def mock_super_post_process(*args, **kwargs):
                    yield "style.css", "style.abc123.css", True

                with patch(
                    "django.contrib.staticfiles.storage.HashedFilesMixin.post_process",
                    mock_super_post_process,
                ):
                    with patch.object(storage, "save_manifest"):
                        list(storage.post_process(paths))

                        # Should not have deleted original files
                        mock_delete.assert_not_called()

    def test_keep_original_files_false(self):
        """Test that original files are deleted when keep_original_files=False"""
        storage = EnhancedManifestStaticFilesStorage(keep_original_files=False)

        with patch.object(storage, "exists", return_value=True):
            with patch.object(storage, "delete") as mock_delete:
                # Simulate post_process
                paths = {"style.css": (storage, "style.css")}

                # Mock the parent post_process to yield processed files
                def mock_super_post_process(*args, **kwargs):
                    yield "style.css", "style.abc123.css", True

                with patch(
                    "django.contrib.staticfiles.storage.HashedFilesMixin.post_process",
                    mock_super_post_process,
                ):
                    with patch.object(storage, "save_manifest"):
                        list(storage.post_process(paths))

                        # Should have deleted original file
                        mock_delete.assert_called_once_with("style.css")


if __name__ == "__main__":
    # Configure Django settings for testing
    import django
    from django.conf import settings as django_settings

    if not django_settings.configured:
        django_settings.configure(
            DEBUG=True,
            SECRET_KEY="test-secret-key",
            STATIC_URL="/static/",
            INSTALLED_APPS=[
                "django.contrib.staticfiles",
                "django_manifeststaticfiles_enhanced",
            ],
            DATABASES={
                "default": {
                    "ENGINE": "django.db.backends.sqlite3",
                    "NAME": ":memory:",
                }
            },
        )
        django.setup()

    unittest.main()
