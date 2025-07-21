"""
Tests for EnhancedManifestStaticFilesStorage
"""

import json
import tempfile
from collections import defaultdict
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


class DependencyGraphTest(TestCase):
    """Test the dependency graph functionality"""

    def setUp(self):
        self.storage = EnhancedManifestStaticFilesStorage()

    def test_build_dependency_graph(self):
        """Test building dependency graph from file paths"""
        # Create mock storage and paths
        mock_storage = MockStorage()

        # Add CSS file with imports
        mock_storage.files["css/main.css"] = (
            b"@import url('base.css'); body { background: url('../img/bg.png'); }"
        )

        # Add JS file with imports
        mock_storage.files["js/app.js"] = (
            b"import { Component } from './components.js';"
        )

        # Add file with source map
        mock_storage.files["js/library.js"] = (
            b"function test() {}\n//# sourceMappingURL=library.js.map"
        )

        # Add files that don't need adjustment
        mock_storage.files["img/bg.png"] = b"PNG content"
        mock_storage.files["css/base.css"] = b"body { color: black; }"
        mock_storage.files["js/components.js"] = b"export class Component {}"
        mock_storage.files["js/library.js.map"] = b'{"version": 3}'

        paths = {
            "css/main.css": (mock_storage, "css/main.css"),
            "js/app.js": (mock_storage, "js/app.js"),
            "js/library.js": (mock_storage, "js/library.js"),
            "img/bg.png": (mock_storage, "img/bg.png"),
            "css/base.css": (mock_storage, "css/base.css"),
            "js/components.js": (mock_storage, "js/components.js"),
            "js/library.js.map": (mock_storage, "js/library.js.map"),
        }

        # Patch _should_adjust_url and _get_target_name to simulate expected behavior
        with patch.object(
            self.storage,
            "_should_adjust_url",
            lambda url: True if "." in url else False,
        ):
            graph, non_adjustable = self.storage._build_dependency_graph(paths)

            # Check that graph contains all files
            self.assertEqual(len(graph), 7)

            # Check that files with dependencies are properly connected
            self.assertIn("css/main.css", graph)
            self.assertIn("css/base.css", graph["css/main.css"]["dependencies"])
            self.assertIn("img/bg.png", graph["css/main.css"]["dependencies"])

            self.assertIn("js/app.js", graph)
            self.assertIn("js/components.js", graph["js/app.js"]["dependencies"])

            self.assertIn("js/library.js", graph)
            self.assertIn("js/library.js.map", graph["js/library.js"]["dependencies"])

            # Check that dependents are correctly recorded
            self.assertIn("css/main.css", graph["css/base.css"]["dependents"])
            self.assertIn("css/main.css", graph["img/bg.png"]["dependents"])
            self.assertIn("js/app.js", graph["js/components.js"]["dependents"])
            self.assertIn("js/library.js", graph["js/library.js.map"]["dependents"])

            # Check non-adjustable files
            self.assertEqual(
                non_adjustable,
                {"img/bg.png", "css/base.css", "js/components.js", "js/library.js.map"},
            )

            # Check needs_adjustment flag
            self.assertTrue(graph["css/main.css"]["needs_adjustment"])
            self.assertTrue(graph["js/app.js"]["needs_adjustment"])
            self.assertTrue(graph["js/library.js"]["needs_adjustment"])


class TopologicalSortTest(TestCase):
    """Test the topological sort functionality"""

    def setUp(self):
        self.storage = EnhancedManifestStaticFilesStorage()

    def test_topological_sort(self):
        """Test sorting files in dependency order"""
        # Create a test graph
        graph = defaultdict(
            lambda: {
                "dependencies": set(),
                "dependents": set(),
                "needs_adjustment": True,
                "url_positions": [],
            }
        )

        # Set up a simple dependency chain: A -> B -> C -> D
        graph["A"] = {
            "dependencies": {"B"},
            "dependents": set(),
            "needs_adjustment": True,
            "url_positions": [("B", 0)],
        }
        graph["B"] = {
            "dependencies": {"C"},
            "dependents": {"A"},
            "needs_adjustment": True,
            "url_positions": [("C", 0)],
        }
        graph["C"] = {
            "dependencies": {"D"},
            "dependents": {"B"},
            "needs_adjustment": True,
            "url_positions": [("D", 0)],
        }
        graph["D"] = {
            "dependencies": set(),
            "dependents": {"C"},
            "needs_adjustment": True,
            "url_positions": [],
        }

        # Add some non-adjustable files
        graph["E"] = {
            "dependencies": set(),
            "dependents": set(),
            "needs_adjustment": False,
            "url_positions": [],
        }
        non_adjustable = {"E"}

        # Sort the graph
        result, circular = self.storage._topological_sort(graph, non_adjustable)

        # Check the order is correct (D -> C -> B -> A)
        self.assertEqual(result, ["D", "C", "B", "A"])
        self.assertEqual(circular, {})

    def test_topological_sort_with_circular_dependencies(self):
        """Test sorting with circular dependencies"""
        # Create a test graph with a circular dependency: A -> B -> C -> A
        graph = defaultdict(
            lambda: {
                "dependencies": set(),
                "dependents": set(),
                "needs_adjustment": True,
                "url_positions": [],
            }
        )

        graph["A"] = {
            "dependencies": {"B"},
            "dependents": {"C"},
            "needs_adjustment": True,
            "url_positions": [("B", 0)],
        }
        graph["B"] = {
            "dependencies": {"C"},
            "dependents": {"A"},
            "needs_adjustment": True,
            "url_positions": [("C", 0)],
        }
        graph["C"] = {
            "dependencies": {"A"},
            "dependents": {"B"},
            "needs_adjustment": True,
            "url_positions": [("A", 0)],
        }

        # Add a separate linear chain: D -> E
        graph["D"] = {
            "dependencies": {"E"},
            "dependents": set(),
            "needs_adjustment": True,
            "url_positions": [("E", 0)],
        }
        graph["E"] = {
            "dependencies": set(),
            "dependents": {"D"},
            "needs_adjustment": True,
            "url_positions": [],
        }

        non_adjustable = set()

        # Sort the graph
        result, circular = self.storage._topological_sort(graph, non_adjustable)

        # Linear chain should be processed (E -> D)
        self.assertEqual(result, ["E", "D"])

        # Check circular dependencies
        self.assertEqual(set(circular.keys()), {"A", "B", "C"})
        self.assertEqual(set(circular["A"]), {"B"})
        self.assertEqual(set(circular["B"]), {"C"})
        self.assertEqual(set(circular["C"]), {"A"})


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
