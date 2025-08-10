"""
Django ManifestStaticFiles Enhanced

Enhanced ManifestStaticFilesStorage for Django with improvements from
Django tickets: 27929, 21080, 26583, 28200, 34322
"""

__version__ = "0.4.5"

from .storage import EnhancedManifestStaticFilesStorage

__all__ = ["EnhancedManifestStaticFilesStorage"]
