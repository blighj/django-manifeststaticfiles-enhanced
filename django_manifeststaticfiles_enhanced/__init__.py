"""
Django ManifestStaticFiles Enhanced

Enhanced ManifestStaticFilesStorage for Django with improvements from
Django tickets: 27929, 21080, 26583, 28200, 34322, 23517
"""

__version__ = "0.7.2"

from .storage import (
    EnhancedManifestStaticFilesStorage,
    TestingManifestStaticFilesStorage,
)

__all__ = ["EnhancedManifestStaticFilesStorage", "TestingManifestStaticFilesStorage"]
