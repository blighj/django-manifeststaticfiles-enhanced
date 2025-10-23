import os
import posixpath
import re
import textwrap
import threading
from concurrent.futures import ThreadPoolExecutor
from graphlib import CycleError, TopologicalSorter
from urllib.parse import unquote, urldefrag, urlsplit, urlunsplit

import django
from django.conf import settings
from django.contrib.staticfiles import finders
from django.contrib.staticfiles.storage import (
    HashedFilesMixin,
    ManifestFilesMixin,
    StaticFilesStorage,
)
from django.contrib.staticfiles.utils import matches_patterns
from django.core.exceptions import ImproperlyConfigured
from django.core.files.base import ContentFile

from django_manifeststaticfiles_enhanced.jslex import (
    extract_css_urls,
    find_import_export_strings,
)

# Global lock for directory creation to ensure thread-safety across all instances
_makedirs_lock = threading.Lock()

# Global lock for hashed_files dictionary updates during parallel post-processing
_hashed_files_lock = threading.Lock()


class ThreadSafeStorageMixin:
    """
    Mixin to make FileSystemStorage thread-safe for parallel operations.

    Django's FileSystemStorage._save() manipulates umask when creating directories,
    which is a process-wide setting and not thread-safe. This mixin overrides _save()
    to serialize directory creation operations while keeping file I/O parallel.
    """

    def _save(self, name, content):
        """
        Thread-safe version of _save that prevents race conditions in dir creation.

        This completely replaces Django's _save to avoid the umask race condition.
        """
        from django.core.files import locks
        from django.core.files.move import file_move_safe

        full_path = self.path(name)
        directory = os.path.dirname(full_path)

        # Thread-safe directory creation - always lock to serialize umask manipulation
        with _makedirs_lock:
            try:
                if self.directory_permissions_mode is not None:
                    old_umask = os.umask(0o777 & ~self.directory_permissions_mode)
                    try:
                        os.makedirs(
                            directory, self.directory_permissions_mode, exist_ok=True
                        )
                    finally:
                        os.umask(old_umask)
                    # CRITICAL: Always explicitly set permissions after makedirs!
                    # Even when creating new directories, the mode parameter to
                    # os.makedirs() is affected by the process umask. Since umask is
                    # process-wide and can be modified by other threads, we must
                    # explicitly chmod to guarantee exact permissions.
                    os.chmod(directory, self.directory_permissions_mode)
                else:
                    os.makedirs(directory, exist_ok=True)
            except FileExistsError:
                raise FileExistsError("%s exists and is not a directory." % directory)

        # File saving logic (copied from Django's FileSystemStorage._save)
        # but without the directory creation part
        while True:
            try:
                if hasattr(content, "temporary_file_path"):
                    # Use file_move_safe for uploaded files with temp paths
                    file_move_safe(content.temporary_file_path(), full_path)
                else:
                    # Stream the content to the file
                    if django.VERSION[:2] in [(4, 2), (5, 0)]:
                        open_flags = self.OS_OPEN_FLAGS
                    else:
                        open_flags = (
                            os.O_WRONLY
                            | os.O_CREAT
                            | os.O_EXCL
                            | getattr(os, "O_BINARY", 0)
                        )
                    fd = os.open(full_path, open_flags, 0o666)
                    _file = None
                    try:
                        locks.lock(fd, locks.LOCK_EX)
                        for chunk in content.chunks():
                            if _file is None:
                                mode = "wb" if isinstance(chunk, bytes) else "wt"
                                _file = os.fdopen(fd, mode)
                            _file.write(chunk)
                    finally:
                        locks.unlock(fd)
                        if _file is not None:
                            _file.close()
                        else:
                            os.close(fd)
            except FileExistsError:
                # A new name is needed if the file exists
                name = self.get_available_name(name)
                full_path = self.path(name)
            else:
                # Set file permissions if specified
                if self.file_permissions_mode is not None:
                    os.chmod(full_path, self.file_permissions_mode)
                # Store filenames with forward slashes, even on Windows
                return name.replace("\\", "/")


class DebugValidationMixin:
    """
    Mixin to validate static file paths that though valid for
    StaticFileStorage are not valid for ManifestStaticFileStorage
    """

    def _validate_url(self, name, force=False):
        """
        Return the URL for a static file.

        This will validate the static path to catch common issues that would
        prevent the manifest key lookup, normally these would only appear in
        production when debug=True, this mixin allows us to cover them in
        development and testing.
        """

        # 1. Check for paths starting with /
        if name.startswith("/"):
            raise ValueError(f"Static paths should not start with '/' ({name}). ")

        # 2. Check for paths using backslashes
        if "\\" in name:
            raise ValueError(f"Static paths should not use backslashes ({name}). ")

        # Clean and normalize the path for further checks
        normalized_path = posixpath.normpath(name)

        # 3 Check if the file exists
        absolute_path = finders.find(normalized_path)
        if not absolute_path and self.manifest_strict:
            raise ValueError(f"Static file not found ({name}). ")

        # 4 Check for case sensitivity issues
        file_name = os.path.basename(name)
        dir_path = os.path.dirname(absolute_path)

        if dir_path and os.path.exists(dir_path):
            # Get actual files in the directory with their exact case
            actual_files = os.listdir(dir_path)
            if file_name not in actual_files and file_name.lower() in [
                f.lower() for f in actual_files
            ]:
                raise ValueError(f"Static file has incorrect case ({name}). ")

        # Call the parent url method
        return super().url(name, force)


class ProcessingException(Exception):
    def __init__(self, e, file_name):
        self.file_name = file_name
        self.original_exception = e
        super().__init__(e.args[0] if len(e.args) else "")


class EnhancedHashedFilesMixin(DebugValidationMixin, HashedFilesMixin):

    def url(self, name, force=False):
        if settings.DEBUG and not force:
            return self._validate_url(name, force)
        return super().url(name, force)

    def post_process(self, paths, dry_run=False, **options):
        """
        Post process the given dictionary of files (called from collectstatic).

        Uses a dependency graph approach to minimize the number of passes required.
        """
        try:
            # if we're in dry run, still find urls and raise any exceptions
            if dry_run:
                self._test_url_substitutions(paths)
                return

            # Process files using the dependency graph
            yield from self._post_process(paths)
        except ProcessingException as exc:
            # django's collectstatic management command is written to expect
            # the exception to be returned in this format
            yield exc.file_name, None, exc.original_exception

    def _test_url_substitutions(self, paths):
        """
        Process the paths for url suvstitutions and find exceptions
        """
        substitutions_dict = self._find_substitutions(paths)
        for name, url_positions in substitutions_dict.items():
            for url, _ in url_positions:
                try:
                    self._adjust_url(url, name, paths)
                except ValueError as exc:
                    if not self._should_ignore_url(name, url):
                        message = exc.args[0] if len(exc.args) else ""
                        message = f"Error processing the url {url}\n{message}"
                        exc = self._make_helpful_exception(ValueError(message), name)
                        raise ProcessingException(exc, name)

    def _post_process(self, paths):
        """
        Process static files using a unified dependency graph approach.

        This method processes files in three phases:
        1. Non-adjustable files in parallel (images, fonts, etc. - no dependencies)
        2. Linear dependencies sequentially (CSS/JS with dependencies)
        3. Circular dependencies with special handling
        """
        hashed_files = {}

        substitutions_dict = self._find_substitutions(paths)
        non_adjustable, linear_deps, circular_deps = self._topological_sort(
            paths, substitutions_dict
        )

        # Phase 1: Process non-adjustable files in parallel
        # These are files with no URL substitutions (images, fonts, etc.)

        max_workers = getattr(self, "post_process_workers", None)

        if non_adjustable and (max_workers is None or max_workers > 1):
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                # Submit all non-adjustable files for parallel processing
                futures = [
                    executor.submit(
                        self._process_file,
                        name,
                        paths[name],
                        hashed_files,
                        [],  # No substitutions for non-adjustable files
                    )
                    for name in non_adjustable
                ]

                # Collect results and update hashed_files with thread safety
                for future in futures:
                    name, hashed_name, processed = future.result()
                    with _hashed_files_lock:
                        hashed_files[self.hash_key(self.clean_name(name))] = hashed_name
                    yield name, hashed_name, processed
        else:
            # Fall back to sequential processing if max_workers=1 or no files
            for name in non_adjustable:
                name, hashed_name, processed = self._process_file(
                    name, paths[name], hashed_files, []
                )
                hashed_files[self.hash_key(self.clean_name(name))] = hashed_name
                yield name, hashed_name, processed

        # Phase 2: Process linear dependencies sequentially
        # These files have dependencies and must be processed in order
        for name in linear_deps:
            name, hashed_name, processed = self._process_file(
                name, paths[name], hashed_files, substitutions_dict.get(name, [])
            )
            hashed_files[self.hash_key(self.clean_name(name))] = hashed_name
            yield name, hashed_name, processed

        # Phase 3: Handle circular dependencies
        if circular_deps:
            circular_hashes = self._process_circular_dependencies(
                circular_deps, paths, substitutions_dict, hashed_files
            )
            for name, hashed_name in circular_hashes:
                hashed_files[self.hash_key(self.clean_name(name))] = hashed_name
                yield name, hashed_name, True

        # Store the processed paths
        self.hashed_files.update(hashed_files)

    def _find_substitutions(self, paths):
        """
        Returns a dictionary mapping file names that need substitutions to a
        list of file names that need substituting along with the position in
        the file
        """
        substitutions_dict = {}
        adjustable_paths = [
            path for path in paths if matches_patterns(path, ["*.css", "*.js"])
        ]
        for name in adjustable_paths:
            storage, path = paths[name]
            with storage.open(path) as original_file:
                try:
                    content = original_file.read().decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise ProcessingException(exc, path)

                substitutions_dict[name] = (
                    self._process_css_urls(name, content)
                    + self._process_js_modules(name, content)
                    + self._process_sourcemap(name, content)
                )
        return substitutions_dict

    def _topological_sort(self, paths, substitutions_dict):
        """
        Examines all the files that need substitutions and returns the list of
        files sorted in an order that is safe to process lineraly, e.g
        image.png is hashed before styles.css needs to replace it with
        image.hash.png in a url().
        Any circular dependencies found will be returned as a seperate list.

        Returns a tuple of (non_adjustable, linear_deps, circular_deps) where:
        - non_adjustable: Files that can be processed in parallel (no dependencies)
        - linear_deps: Files with dependencies that must be processed sequentially
        - circular_deps: Files with circular dependencies (special handling)
        """

        graph_sorter = TopologicalSorter()
        adjustable_paths = substitutions_dict.keys()
        non_adjustable = set(paths.keys()) - set(adjustable_paths)

        # build the graph based on the substitutions_dict
        for name, url_positions in substitutions_dict.items():
            if url_positions:
                for url_name, _ in url_positions:
                    # normalise base.css, /static/base.css, ../base.css, etc
                    target = self._get_target_name(url_name, name)
                    graph_sorter.add(name, target)
            else:
                non_adjustable.add(name)

        try:
            graph_sorter.prepare()
        except CycleError:
            # Even if there is a CycleError we can still access the linear
            # nodes using get_ready
            pass
        linear_deps = []
        while graph_sorter.is_active():
            node_group = graph_sorter.get_ready()
            linear_deps += [node for node in node_group if node in adjustable_paths]
            graph_sorter.done(*node_group)

        def path_level(name):
            return len(name.split(os.sep))

        non_adjustable = sorted(list(non_adjustable), key=path_level, reverse=True)
        circular_deps = set(adjustable_paths) - set(linear_deps) - set(non_adjustable)

        return non_adjustable, linear_deps, circular_deps

    def _process_js_modules(self, name, content):
        """Process JavaScript import/export statements."""
        url_positions = []

        if not self.support_js_module_import_aggregation or not matches_patterns(
            name, ("*.js",)
        ):
            return url_positions

        # simple search rules out most js files quickly
        complex_adjustments = "import" in content or (
            "export" in content and "from" in content
        )

        if not complex_adjustments:
            return url_positions

        # The simple search still leave lots of falst positives,
        # like the words important or exports
        # Match for import export syntax to futher reduce the need
        # to run the lexer, should cut out 90% of false positives
        if not self.import_export_pattern.search(content):
            return url_positions

        try:
            urls = find_import_export_strings(
                content,
                should_ignore_url=lambda url: self._should_ignore_url(name, url),
            )
        except ValueError as e:
            message = e.args[0] if len(e.args) else ""
            message = f"The js file '{name}' could not be processed.\n{message}"
            raise ProcessingException(ValueError(message), name)
        for url_name, position in urls:
            if self._should_adjust_url(url_name):
                url_positions.append((url_name, position))

        return url_positions

    import_export_pattern = re.compile(
        # check for import statements
        r"((^|[;}]|\*/)\s*import\b|"
        # check for dynamic imports
        r"import\s\(|"
        # check for edge case with comment in between import and opening bracket
        r"import\s*/\*.*?\*/\s*\(|"
        # check for the word export must be followed
        r"\bexport[\s{/*])",
        re.MULTILINE,
    )

    def _process_css_urls(self, name, content):
        """Process CSS url & import statements."""
        url_positions = []
        if not matches_patterns(name, ("*.css",)):
            return url_positions
        search_content = content.lower()
        complex_adjustments = "url(" in search_content or "@import" in search_content

        if not complex_adjustments:
            return url_positions

        for url_name, position in extract_css_urls(content):
            if self._should_adjust_url(url_name):
                url_positions.append((url_name, position))
        return url_positions

    def _process_sourcemap(self, name, content):
        url_positions = []
        if "sourceMappingURL" not in content:
            return url_positions

        for extension, pattern in self.source_map_patterns.items():
            if matches_patterns(name, (extension,)):
                for match in pattern.finditer(content):
                    url = match.group("url")
                    if self._should_adjust_url(url):
                        url_positions.append((url, match.start("url")))
        return url_positions

    source_map_patterns = {
        "*.css": re.compile(
            r"(?m)^/\*#[ \t](?-i:sourceMappingURL)=(?P<url>.*?)[ \t]*\*/$",
            re.IGNORECASE,
        ),
        "*.js": re.compile(
            r"(?m)^//# (?-i:sourceMappingURL)=(?P<url>.*?)[ \t]*$", re.IGNORECASE
        ),
    }

    def _should_adjust_url(self, url):
        """
        Return whether this is a url that should be adjusted
        """
        # Ignore absolute/protocol-relative and data-uri URLs.
        if re.match(r"^[a-z]+:", url) or url.startswith("//"):
            return False

        # Ignore absolute URLs that don't point to a static file (dynamic
        # CSS / JS?). Note that STATIC_URL cannot be empty.
        if url.startswith("/") and not url.startswith(settings.STATIC_URL):
            return False

        # Strip off the fragment so a path-like fragment won't interfere.
        url_path, _ = urldefrag(url)

        # Ignore URLs without a path
        if not url_path:
            return False
        return True

    def _adjust_url(self, url, name, hashed_files):
        """
        Return the hashed url without affecting fragments
        """
        # Strip off the fragment so a path-like fragment won't interfere.
        url_path, fragment = urldefrag(url)

        # Strip off query string as well - it shouldn't affect file hash lookup
        parsed = urlsplit(url_path)
        clean_path = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
        query_string = parsed.query

        # determine the target file name (remove /static if needed)
        target_name = self._get_base_target_name(clean_path, name)

        # Determine the hashed name of the target file with the storage backend.
        hashed_url = self._url(
            self._stored_name,
            unquote(target_name),
            force=True,
            hashed_files=hashed_files,
        )

        # Ensure hashed_url is a string (handle mock objects in tests)
        if hasattr(hashed_url, "__str__"):
            hashed_url = str(hashed_url)

        transformed_url = "/".join(
            url_path.split("/")[:-1] + hashed_url.split("/")[-1:]
        )

        # Restore the fragment that was stripped off earlier.
        if query_string:
            transformed_url += "?" + query_string
        if fragment:
            transformed_url += (
                "?#" if "?#" in url and "?" not in transformed_url else "#"
            ) + fragment

        # Ensure we return a string (handle mock objects in tests)
        return str(transformed_url)

    def _get_target_name(self, url, source_name):
        """
        Get the target file name from a URL and source file name
        """
        url_path, _ = urldefrag(url)
        path = posixpath.normpath(self._get_base_target_name(url_path, source_name))
        if os.sep != "/":
            path = path.replace("/", os.sep)
        return path

    def _get_base_target_name(self, url_path, source_name):
        """
        Get the target file name from a URL (no fragment) and source file name
        """
        # Used by _get_target_name and _adjust_url
        if url_path.startswith("/"):
            # Otherwise the condition above would have returned prematurely.
            assert url_path.startswith(settings.STATIC_URL)
            target_name = url_path.removeprefix(settings.STATIC_URL)
        else:
            # We're using the posixpath module to mix paths and URLs conveniently.
            source_name = (
                source_name if os.sep == "/" else source_name.replace(os.sep, "/")
            )
            target_name = posixpath.join(posixpath.dirname(source_name), url_path)
        return target_name

    def _process_file(self, name, storage_and_path, hashed_files, url_positions):
        """
        Process a single file using the unified graph structure.
        """
        storage, path = storage_and_path

        with storage.open(path) as original_file:
            # Calculate hash of original file
            if hasattr(original_file, "seek"):
                original_file.seek(0)

            hashed_name = self.hashed_name(name, original_file)
            hashed_file_exists = self.exists(hashed_name)
            processed = False

            # If this is an adjustable file with URL positions,
            # apply transformations
            if url_positions:
                if hasattr(original_file, "seek"):
                    original_file.seek(0)
                content = original_file.read().decode("utf-8")

                # Apply URL substitutions using stored positions
                content = self._process_file_content(
                    name, content, url_positions, hashed_files
                )

                # Create a content file and calculate its hash
                content_file = ContentFile(content.encode())
                new_hashed_name = self.hashed_name(name, content_file)

                if not self.exists(new_hashed_name):
                    saved_name = self._save(new_hashed_name, content_file)
                    hashed_name = self.clean_name(saved_name)
                else:
                    hashed_name = new_hashed_name

                processed = True

            elif not hashed_file_exists:
                # For non-adjustable files just copy the file
                if hasattr(original_file, "seek"):
                    original_file.seek(0)
                processed = True
                saved_name = self._save(hashed_name, original_file)
                hashed_name = self.clean_name(saved_name)

            return name, hashed_name, processed

    def _process_file_content(self, name, content, url_positions, hashed_files):
        """
        Process file content by substituting URLs.
        url_positions is a list of (url, position) tuples.
        """
        if not url_positions:
            return content

        result_parts = []
        last_position = 0

        # Sort by position to ensure correct order
        sorted_positions = sorted(
            url_positions,
            key=lambda x: x[1],
        )

        for url, pos in sorted_positions:
            position = pos
            # Add content before this URL
            result_parts.append(content[last_position:position])

            try:
                transformed_url = self._adjust_url(url, name, hashed_files)
            except ValueError as exc:
                if self._should_ignore_url(name, url):
                    transformed_url = url
                else:
                    message = exc.args[0] if len(exc.args) else ""
                    message = f"Error processing the url {url}\n{message}"
                    exc = self._make_helpful_exception(ValueError(message), name)
                    raise ProcessingException(exc, name)

            result_parts.append(transformed_url)
            last_position = position + len(url)

        # Add remaining content
        result_parts.append(content[last_position:])
        return "".join(result_parts)

    def _process_circular_dependencies(
        self, circular_deps, paths, substitutions_dict, hashed_files
    ):
        """
        Process files with circular dependencies.

        This method breaks the dependency cycle by:
        1. First replacing all non-circular URLs in each file
        and generating a hash based on their combined content
        2. Apply this stable combined hash to each of the files
        3. Safely updating all the references within the files

        Args:
            circular_deps: Dict mapping files to their circular dependencies
            paths: Dict mapping file paths to (storage, path) tuples
            substitutions_dict: Dictionary of url positions
            hashed_files: Dict of already processed files
        """
        circular_hashes = {}
        processed_files = set()

        # First pass: Replace all non-circular dependency URLs in each file
        # and generate group hash
        group_hash, original_contents = self._calculate_combined_hash(
            circular_deps, paths, substitutions_dict, hashed_files
        )

        # Second pass: Create hashed filenames using the group hash
        for name in circular_deps:
            if name in processed_files:
                continue

            # Generate a hashed filename based on the group hash
            filename, ext = os.path.splitext(name)
            hashed_name = f"{filename}.{group_hash}{ext}"

            # Store the hash for this file
            hash_key = self.hash_key(self.clean_name(name))
            circular_hashes[hash_key] = hashed_name
            processed_files.add(name)

        # Third pass: Process all URLs (including circular ones) and save files
        for name in circular_deps:
            content = original_contents[name]

            combined_hashes = {**hashed_files, **circular_hashes}
            content = self._process_file_content(
                name, content, substitutions_dict.get(name, []), combined_hashes
            )

            # Get the hashed name for this file
            hash_key = self.hash_key(self.clean_name(name))
            hashed_name = circular_hashes[hash_key]

            # Save the processed content to the hashed filename
            content_file = ContentFile(content.encode())
            if self.exists(hashed_name):
                self.delete(hashed_name)
            self._save(hashed_name, content_file)
            yield name, hashed_name

    def _calculate_combined_hash(
        self, circular_deps, paths, substitutions_dict, hashed_files
    ):
        """
        Return a hash of the combined content from all circular dependencies
        Replace the non circular URL's before calculating

        Also returns the original content to save opening it twice
        """
        original_contents = {}
        processed_contents = {}
        for name in circular_deps:
            storage, path = paths[name]
            with storage.open(path) as original_file:
                if hasattr(original_file, "seek"):
                    original_file.seek(0)
                content = original_file.read().decode("utf-8")

                original_contents[name] = content

                # Filter URL positions to only non-circular dependencies
                non_circular_positions = []
                for url, pos in substitutions_dict.get(name, []):
                    target = self._get_target_name(url, name)
                    if target not in circular_deps:
                        non_circular_positions.append((url, pos))
                # Replace all non-circular URLs first
                if non_circular_positions:
                    content = self._process_file_content(
                        name, content, non_circular_positions, hashed_files
                    )

                # Store the processed content for the second pass
                # We haven't actually saved these changes to disk
                processed_contents[name] = content

        # Calculate a stable hash for all circular dependencies combined
        combined_content = "".join(
            processed_contents[name] for name in sorted(circular_deps)
        )
        combined_file = ContentFile(combined_content.encode())
        group_hash = self.file_hash("_combined", combined_file)
        return group_hash, original_contents

    def _make_helpful_exception(self, exception, name):
        """
        The ValueError for missing files, such as images/fonts in css, sourcemaps,
        or js files in imports, lack context of the filebeing processed.
        Reformat them to be more helpful in revealing the source of the problem.
        """
        message = exception.args[0] if len(exception.args) else ""
        match = self._error_msg_re.search(message)
        if match:
            extension = os.path.splitext(name)[1].lstrip(".").upper()
            message = self._error_msg.format(
                orig_message=message,
                filename=name,
                missing=match.group(2),
                ext=extension,
                url=match.group(1),
            )
            exception = ValueError(message)
        return exception

    _error_msg_re = re.compile(
        r"^Error processing the url (.+)\nThe file '(.+)' could not be found"
    )

    _error_msg = textwrap.dedent(
        """\
        {orig_message}

        The {ext} file '{filename}' references a file which could not be found:
          {missing}

        Please check the URL references in this {ext} file, particularly any
        relative paths which might be pointing to the wrong location.
        It is possible to ignore this error by pasing the OPTIONS:
        {{
            "ignore_errors": ["{filename}:{url}"]
        }}
        """
    )

    def _should_ignore_url(self, filename, url):
        """
        Check if the error for this file should be ignored
        based on the ignore_errors setting.

        Format for ignore_errors entries: "file:url" where:
        - 'file' is the filename pattern (can use * as wildcard)
        - 'url' is the missing url pattern (can use * as wildcard)
        """
        # Check if any ignore pattern matches
        for pattern in self.ignore_errors:
            try:
                if ":" not in pattern:
                    continue

                file_pattern, url_pattern = pattern.split(":", 1)

                # Convert glob patterns to regex patterns
                file_regex = self._glob_to_regex(file_pattern.strip())
                url_regex = self._glob_to_regex(url_pattern.strip())

                # Check if both the file and URL match their patterns
                if re.match(file_regex, filename) and re.match(url_regex, url):
                    return True
            except Exception:
                # If pattern matching fails, continue with the next pattern
                continue

        return False

    def _glob_to_regex(self, pattern):
        """
        Convert a glob pattern to a regex pattern.
        """
        regex = ""
        i, n = 0, len(pattern)

        while i < n:
            c = pattern[i]
            i += 1

            if c == "*":
                regex += ".*"
            elif c in ".$^+[](){}|\\":
                regex += "\\" + c
            else:
                regex += c

        return "^" + regex + "$"


class EnhancedManifestFilesMixin(EnhancedHashedFilesMixin, ManifestFilesMixin):
    """
    Enhanced ManifestFilesMixin with keep_original_files option (ticket_27929).
    """

    keep_original_files = True

    def post_process(self, *args, **kwargs):
        self.hashed_files = {}
        yield from super().post_process(*args, **kwargs)
        if not kwargs.get("dry_run"):
            self.save_manifest()


class EnhancedManifestStaticFilesStorage(
    ThreadSafeStorageMixin, EnhancedManifestFilesMixin, StaticFilesStorage
):
    """
    Enhanced ManifestStaticFilesStorage:

    - ticket_21080: CSS lexer for better URL parsing
    - ticket_27929: keep_original_files option
    - ticket_28200: Optimized storage to avoid unnecessary file operations
    - ticket_34322: JsLex for ES module support
    - ignore_errors: List of 'file:url' errors to ignore during post-processing
    - Thread-safe storage for parallel collectstatic operations
    """

    def __init__(
        self,
        location=None,
        base_url=None,
        support_js_module_import_aggregation=None,
        manifest_name=None,
        manifest_strict=None,
        keep_original_files=None,
        ignore_errors=None,
        *args,
        **kwargs,
    ):
        # Set configurable attributes as instance attributes if provided
        if support_js_module_import_aggregation is not None:
            self.support_js_module_import_aggregation = (
                support_js_module_import_aggregation
            )
        if manifest_name is not None:
            self.manifest_name = manifest_name
        if manifest_strict is not None:
            self.manifest_strict = manifest_strict
        if keep_original_files is not None:
            self.keep_original_files = keep_original_files
        if ignore_errors is not None:
            if not isinstance(ignore_errors, list):
                raise ImproperlyConfigured("ignore_errors must be a list")
            self.ignore_errors = ignore_errors
        else:
            self.ignore_errors = []
        super().__init__(location, base_url, *args, **kwargs)


class ThreadSafeStaticFilesStorage(ThreadSafeStorageMixin, StaticFilesStorage):
    """
    StaticFilesStorage with thread-safe directory creation for parallel collectstatic.

    This is a simple wrapper around Django's StaticFilesStorage that adds thread-safe
    directory creation. Use this when you need parallel collectstatic but don't need
    manifest/hashed file features.
    """

    pass


class TestingManifestStaticFilesStorage(DebugValidationMixin, StaticFilesStorage):
    def __init__(
        self,
        location=None,
        base_url=None,
        support_js_module_import_aggregation=None,
        manifest_name=None,
        manifest_strict=None,
        keep_original_files=None,
        ignore_errors=None,
        *args,
        **kwargs,
    ):
        if manifest_strict is not None:
            self.manifest_strict = manifest_strict
        else:
            self.manifest_strict = True
        super().__init__(location, base_url, *args, **kwargs)

    def url(self, name, force=False):
        return self._validate_url(name, force)
