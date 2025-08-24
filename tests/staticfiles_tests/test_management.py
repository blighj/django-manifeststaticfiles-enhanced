import os

from django.conf import STATICFILES_STORAGE_ALIAS, settings
from django.test import override_settings

from .cases import CollectionTestCase


class TestNoFilesCreated:
    def test_no_files_created(self):
        """
        Make sure no files were create in the destination directory.
        """
        self.assertEqual(os.listdir(settings.STATIC_ROOT), [])


class TestCollectionDryRun(TestNoFilesCreated, CollectionTestCase):
    """
    Test ``--dry-run`` option for ``collectstatic`` management command.
    """

    def run_collectstatic(self):
        super().run_collectstatic(dry_run=True)


@override_settings(
    STORAGES={
        **settings.STORAGES,
        STATICFILES_STORAGE_ALIAS: {
            "BACKEND": "django.contrib.staticfiles.storage.ManifestStaticFilesStorage"
        },
    }
)
class TestCollectionDryRunManifestStaticFilesStorage(TestCollectionDryRun):
    pass
