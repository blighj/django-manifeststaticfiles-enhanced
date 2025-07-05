#!/usr/bin/env python
"""
Test runner for django-manifeststaticfiles-enhanced
"""

import argparse
import os
import sys

import django
from django.conf import settings
from django.test.utils import get_runner


def setup_test_environment():
    """Setup Django test environment"""
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "tests.staticfiles_tests.settings")

    # Add the tests directory to sys.path so imports work without 'tests.' prefix
    tests_path = os.path.join(os.path.dirname(__file__), "tests")
    if tests_path not in sys.path:
        sys.path.insert(0, tests_path)

    django.setup()


def run_tests(test_path=None):
    """Run the test suite"""
    setup_test_environment()
    TestRunner = get_runner(settings)
    test_runner = TestRunner(verbosity=2, interactive=True)

    if test_path:
        # Convert path format to Django test format
        if not test_path.startswith("tests."):
            test_path = f"tests.staticfiles_tests.{test_path}"
        test_labels = [test_path]
    else:
        # Run all tests if no specific path provided
        test_labels = ["tests.staticfiles_tests"]

    failures = test_runner.run_tests(test_labels)
    if failures:
        sys.exit(bool(failures))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run Django tests")
    parser.add_argument(
        "test_path",
        nargs="?",
    )
    args = parser.parse_args()

    run_tests(args.test_path)
