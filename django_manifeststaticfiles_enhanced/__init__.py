"""
Django ManifestStaticFiles Enhanced

Enhanced ManifestStaticFilesStorage for Django with improvements from
Django tickets: 27929, 21080, 26583, 28200, 34322
"""

__version__ = "0.6.0"

from .storage import (
    EnhancedManifestStaticFilesStorage,
    TestingManifestStaticFilesStorage,
)

__all__ = ["EnhancedManifestStaticFilesStorage", "TestingManifestStaticFilesStorage"]
