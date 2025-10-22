"""
Parallel collectstatic management command for improved performance.

This extends Django's collectstatic command to process files in parallel,
significantly reducing collection time for projects with many static files.
"""

import os
import threading
from concurrent.futures import ThreadPoolExecutor

from django.contrib.staticfiles.finders import get_finders
from django.contrib.staticfiles.management.commands.collectstatic import (
    Command as DjangoCollectstaticCommand,
)
from django.core.management.base import CommandError

# Global lock for directory creation in link operations
_link_makedirs_lock = threading.Lock()


class Command(DjangoCollectstaticCommand):
    """
    Extended collectstatic command with parallel file processing.

    This implementation improves performance by:
    1. Sequential file discovery (preserves Django's deduplication semantics)
    2. Parallel file processing (copy/link operations)
    3. Thread-safe state management

    The approach ensures correctness by discovering and deduplicating all files
    first, then processing only the unique files in parallel. This avoids race
    conditions and guarantees that the "first finder wins" rule is preserved.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Add Django 6.0 file tracking lists if not present
        if not hasattr(self, "skipped_files"):
            self.skipped_files = []
        if not hasattr(self, "deleted_files"):
            self.deleted_files = []

    def add_arguments(self, parser):
        super().add_arguments(parser)
        parser.add_argument(
            "--parallel",
            type=int,
            default=None,
            dest="parallel_workers",
            help="Number of parallel workers for file processing.",
        )

    def set_options(self, **options):
        """Set instance variables based on an options dict."""
        super().set_options(**options)
        self.parallel_workers = options.get("parallel_workers", None)

        # Use sets for O(1) lookups during parallel processing
        # These will be converted back to lists in collect() for Django compatibility
        if self.parallel_workers is None or self.parallel_workers > 1:
            self._copied_set = set()
            self._symlinked_set = set()
            self._unmodified_set = set()
            self._copied_lock = threading.Lock()
            self._symlinked_lock = threading.Lock()
            self._unmodified_lock = threading.Lock()

    def collect(self):
        """
        Perform the bulk of the work of collectstatic with parallel processing.

        Phase 1: Sequential discovery and deduplication
        Phase 2: Parallel file processing
        Phase 3: Sequential post-processing
        """
        if self.symlink and not self.local:
            raise CommandError("Can't symlink to a remote destination.")

        if self.clear:
            self.clear_dir("")

        # Determine the handler (copy or symlink)
        if self.symlink:
            handler = self._thread_safe_link_file
        else:
            handler = self._thread_safe_copy_file

        # Phase 1: Sequential discovery and deduplication
        found_files = {}
        finder_of_found_files = {}
        work_queue = []

        for finder in get_finders():
            for path, storage in finder.list(self.ignore_patterns):
                # Prefix the relative path if the source storage contains it
                if getattr(storage, "prefix", None):
                    prefixed_path = os.path.join(storage.prefix, path)
                else:
                    prefixed_path = path

                if prefixed_path not in found_files:
                    found_files[prefixed_path] = (storage, path)
                    finder_of_found_files[prefixed_path] = finder
                    work_queue.append((path, prefixed_path, storage))
                else:
                    # if the duplicate is from the same storage warn louder
                    level = 1 if finder == finder_of_found_files[prefixed_path] else 2
                    self.log(
                        "Found another file with the destination path '%s'. It "
                        "will be ignored since only the first encountered file "
                        "is collected. If this is not what you want, make sure "
                        "every static file has a unique path. "
                        "The file at '%s' will be used; '%s' will be skipped."
                        % (
                            prefixed_path,
                            found_files[prefixed_path][0].location,
                            storage.location,
                        ),
                        level=level,
                    )
                    self.skipped_files.append(prefixed_path)

        # Phase 2: Parallel file processing
        if self.parallel_workers is None or self.parallel_workers > 1 and work_queue:
            self._process_files_parallel(work_queue, handler)
        else:
            # Fall back to sequential processing if parallel_workers=1
            for path, prefixed_path, storage in work_queue:
                handler(path, prefixed_path, storage)

        # Convert sets back to lists for Django compatibility
        if self.parallel_workers is None or self.parallel_workers > 1:
            self.copied_files = list(self._copied_set)
            self.symlinked_files = list(self._symlinked_set)
            self.unmodified_files = list(self._unmodified_set)

        # Phase 3: Sequential post-processing (unchanged from Django)
        if self.post_process and hasattr(self.storage, "post_process"):
            processor = self.storage.post_process(found_files, dry_run=self.dry_run)
            for original_path, processed_path, processed in processor:
                if isinstance(processed, Exception):
                    self.stderr.write("Post-processing '%s' failed!" % original_path)
                    self.stderr.write()
                    raise processed
                if processed:
                    self.log(
                        "Post-processed '%s' as '%s'" % (original_path, processed_path),
                        level=2,
                    )
                    self.post_processed_files.append(original_path)
                else:
                    self.log("Skipped post-processing '%s'" % original_path, level=2)

        return {
            "modified": self.copied_files + self.symlinked_files,
            "unmodified": self.unmodified_files,
            "post_processed": self.post_processed_files,
            "deleted": self.deleted_files,
            "skipped": self.skipped_files,
        }

    def _process_files_parallel(self, work_queue, handler):
        """
        Process files in parallel using ThreadPoolExecutor.

        Args:
            work_queue: List of (path, prefixed_path, storage) tuples
            handler: The file handler function (copy or link)
        """
        with ThreadPoolExecutor(max_workers=self.parallel_workers) as executor:
            # Submit all tasks
            futures = [
                executor.submit(handler, path, prefixed_path, storage)
                for path, prefixed_path, storage in work_queue
            ]

            # Wait for all tasks to complete and handle any exceptions
            for future in futures:
                try:
                    future.result()
                except Exception as exc:
                    # Let exceptions propagate (Django will handle them)
                    raise exc

    def _thread_safe_copy_file(self, path, prefixed_path, source_storage):
        """
        Thread-safe wrapper for copy_file.

        Manages locks around shared state (self.copied_files, self.unmodified_files).
        """
        # The parent copy_file modifies shared state, so we need to protect it
        result = self._copy_file_internal(path, prefixed_path, source_storage)
        return result

    def _thread_safe_link_file(self, path, prefixed_path, source_storage):
        """
        Thread-safe wrapper for link_file.

        Manages locks around shared state (self.symlinked_files, self.unmodified_files).
        """
        result = self._link_file_internal(path, prefixed_path, source_storage)
        return result

    def _copy_file_internal(self, path, prefixed_path, source_storage):
        """
        Internal copy_file implementation with thread-safe state management.

        This is a reimplementation of the parent's copy_file method with proper locking.
        """
        # Skip this file if it was already copied earlier
        if self.parallel_workers is None or self.parallel_workers > 1:
            with self._copied_lock:
                if prefixed_path in self._copied_set:
                    self.log("Skipping '%s' (already copied earlier)" % path, level=2)
                    return
        else:
            if prefixed_path in self.copied_files:
                self.log("Skipping '%s' (already copied earlier)" % path, level=2)
                return

        # Check if we should skip copying original files when keep_original_files=False
        # All files will get hashed versions during post-processing
        if not getattr(self.storage, "keep_original_files", True):
            self.log(
                "Skipping '%s' (will only be saved as hashed version)" % path, level=2
            )
            return

        # Delete the target file if needed or break
        if not self.delete_file(path, prefixed_path, source_storage):
            return

        # The full path of the source file
        source_path = source_storage.path(path)

        # Finally start copying
        if self.dry_run:
            self.log("Pretending to copy '%s'" % source_path, level=1)
        else:
            self.log("Copying '%s'" % source_path, level=2)
            with source_storage.open(path) as source_file:
                self.storage.save(prefixed_path, source_file)

        if self.parallel_workers is None or self.parallel_workers > 1:
            with self._copied_lock:
                self._copied_set.add(prefixed_path)
        else:
            self.copied_files.append(prefixed_path)

    def _link_file_internal(self, path, prefixed_path, source_storage):
        """
        Internal link_file implementation with thread-safe state management.

        This is a reimplementation of the parent's link_file method with proper locking.
        """
        # Skip this file if it was already linked earlier
        if self.parallel_workers is None or self.parallel_workers > 1:
            with self._symlinked_lock:
                if prefixed_path in self._symlinked_set:
                    self.log("Skipping '%s' (already linked earlier)" % path, level=2)
                    return
        else:
            if prefixed_path in self.symlinked_files:
                self.log("Skipping '%s' (already linked earlier)" % path, level=2)
                return

        # Check if we should skip linking original files when keep_original_files=False
        # All files will get hashed versions during post-processing
        if not getattr(self.storage, "keep_original_files", True):
            self.log(
                "Skipping '%s' (will only be saved as hashed version)" % path, level=2
            )
            return

        # Delete the target file if needed or break
        if not self.delete_file(path, prefixed_path, source_storage):
            return

        # The full path of the source file
        source_path = source_storage.path(path)

        # Finally link the file
        if self.dry_run:
            self.log("Pretending to link '%s'" % source_path, level=1)
        else:
            self.log("Linking '%s'" % source_path, level=2)
            full_path = self.storage.path(prefixed_path)
            directory = os.path.dirname(full_path)

            # Thread-safe directory creation for symlinks
            # Always lock in parallel mode to serialize umask manipulation
            if self.parallel_workers is None or self.parallel_workers > 1:
                with _link_makedirs_lock:
                    dir_perms = getattr(
                        self.storage, "directory_permissions_mode", None
                    )
                    if dir_perms is not None:
                        old_umask = os.umask(0o777 & ~dir_perms)
                        try:
                            os.makedirs(directory, dir_perms, exist_ok=True)
                        finally:
                            os.umask(old_umask)
                        # CRITICAL: Always explicitly set permissions after makedirs!
                        # Even when creating new directories, the mode parameter to
                        # os.makedirs() is affected by the process umask. Since umask
                        # is process-wide and can be modified by other threads, we must
                        # explicitly chmod to guarantee exact permissions.
                        os.chmod(directory, dir_perms)
                    else:
                        os.makedirs(directory, exist_ok=True)
            else:
                os.makedirs(directory, exist_ok=True)

            try:
                if os.path.lexists(full_path):
                    os.unlink(full_path)
                os.symlink(source_path, full_path)
            except NotImplementedError:
                import platform

                raise CommandError(
                    "Symlinking is not supported in this "
                    "platform (%s)." % platform.platform()
                )
            except OSError as e:
                raise CommandError(e)

        if self.parallel_workers is None or self.parallel_workers > 1:
            with self._symlinked_lock:
                self._symlinked_set.add(prefixed_path)
        else:
            self.symlinked_files.append(prefixed_path)

    def delete_file(self, path, prefixed_path, source_storage):
        """
        Thread-safe version of delete_file.

        Check if the target file should be deleted if it already exists.
        This method is called by both copy and link operations, so we need
        to ensure thread safety for the unmodified_files list.
        """
        if self.storage.exists(prefixed_path):
            try:
                # When was the target file modified last time?
                target_last_modified = self.storage.get_modified_time(prefixed_path)
            except (OSError, NotImplementedError, AttributeError):
                # The storage doesn't support get_modified_time() or failed
                pass
            else:
                try:
                    # When was the source file modified last time?
                    source_last_modified = source_storage.get_modified_time(path)
                except (OSError, NotImplementedError, AttributeError):
                    pass
                else:
                    # The full path of the target file
                    if self.local:
                        full_path = self.storage.path(prefixed_path)
                        # If it's --link mode and the path isn't a link (i.e.
                        # the previous collectstatic wasn't with --link) or if
                        # it's non-link mode and the path is a link (i.e. the
                        # previous collectstatic was with --link), the old
                        # links/files must be deleted so it's not safe to skip
                        # unmodified files.
                        can_skip_unmodified_files = not (
                            self.symlink ^ os.path.islink(full_path)
                        )
                    else:
                        # In remote storages, skipping is only based on the
                        # modified times since symlinks aren't relevant.
                        can_skip_unmodified_files = True

                    # Avoid sub-second precision (see #14665, #19540)
                    file_is_unmodified = target_last_modified.replace(
                        microsecond=0
                    ) >= source_last_modified.replace(microsecond=0)

                    if file_is_unmodified and can_skip_unmodified_files:
                        if self.parallel_workers is None or self.parallel_workers > 1:
                            with self._unmodified_lock:
                                self._unmodified_set.add(prefixed_path)
                        else:
                            if prefixed_path not in self.unmodified_files:
                                self.unmodified_files.append(prefixed_path)
                        self.log("Skipping '%s' (not modified)" % path, level=2)
                        return False

            # Then delete the existing file if really needed
            if self.dry_run:
                self.log("Pretending to delete '%s'" % path, level=2)
            else:
                self.log("Deleting '%s'" % path, level=2)
                self.storage.delete(prefixed_path)

        return True

    def handle(self, **options):
        """
        Override handle() to provide Django 6.0-style output for all Django versions.

        This backports the improved summary format from Django 6.0 that includes
        deleted and skipped file counts.
        """

        self.set_options(**options)
        message = ["\n"]
        if self.dry_run:
            message.append(
                "You have activated the --dry-run option so no files will be "
                "modified.\n\n"
            )

        message.append(
            "You have requested to collect static files at the destination\n"
            "location as specified in your settings"
        )

        if self.is_local_storage() and self.storage.location:
            destination_path = self.storage.location
            message.append(":\n\n    %s\n\n" % destination_path)
            should_warn_user = self.storage.exists(destination_path) and any(
                self.storage.listdir(destination_path)
            )
        else:
            destination_path = None
            message.append(".\n\n")
            # Destination files existence not checked; play it safe and warn.
            should_warn_user = True

        if self.interactive and should_warn_user:
            if self.clear:
                message.append("This will DELETE ALL FILES in this location!\n")
            else:
                message.append("This will overwrite existing files!\n")

            message.append(
                "Are you sure you want to do this?\n\n"
                "Type 'yes' to continue, or 'no' to cancel: "
            )
            if input("".join(message)) != "yes":
                raise CommandError("Collecting static files cancelled.")

        collected = self.collect()

        if self.verbosity >= 1:
            deleted_count = len(collected["deleted"])
            modified_count = len(collected["modified"])
            unmodified_count = len(collected["unmodified"])
            post_processed_count = len(collected["post_processed"])
            skipped_count = len(collected["skipped"])
            return (
                "\n%(deleted)s%(modified_count)s %(identifier)s %(action)s"
                "%(destination)s%(unmodified)s%(post_processed)s%(skipped)s."
            ) % {
                "deleted": (
                    "%s static file%s deleted, "
                    % (deleted_count, "" if deleted_count == 1 else "s")
                    if deleted_count > 0
                    else ""
                ),
                "modified_count": modified_count,
                "identifier": "static file" + ("" if modified_count == 1 else "s"),
                "action": "symlinked" if self.symlink else "copied",
                "destination": (
                    " to '%s'" % destination_path if destination_path else ""
                ),
                "unmodified": (
                    ", %s unmodified" % unmodified_count
                    if collected["unmodified"]
                    else ""
                ),
                "post_processed": (
                    collected["post_processed"]
                    and ", %s post-processed" % post_processed_count
                    or ""
                ),
                "skipped": (
                    ", %s skipped due to conflict" % skipped_count
                    if collected["skipped"]
                    else ""
                ),
            }
