import os
import posixpath
import re
import textwrap
from collections import defaultdict
from urllib.parse import unquote, urldefrag

import django
from django.conf import STATICFILES_STORAGE_ALIAS, settings
from django.contrib.staticfiles.storage import (
    HashedFilesMixin,
    ManifestFilesMixin,
    StaticFilesStorage,
)
from django.contrib.staticfiles.utils import matches_patterns
from django.core.files.base import ContentFile

from django_manifeststaticfiles_enhanced.jslex import (
    extract_css_urls,
    find_import_export_strings,
)


class EnhancedHashedFilesMixin(HashedFilesMixin):
    support_js_module_import_aggregation = True

    def post_process(self, paths, dry_run=False, **options):
        """
        Post process the given dictionary of files (called from collectstatic).

        Uses a dependency graph approach to minimize the number of passes required.
        """
        # don't even dare to process the files if we're in dry run mode
        if dry_run:
            return

        # Process files using the dependency graph
        yield from self._process_with_dependency_graph(paths, dry_run, **options)

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

        if url_path.startswith("/"):
            # _should_adjust_url would have checked this.
            assert url_path.startswith(settings.STATIC_URL)
            target_name = url_path[len(settings.STATIC_URL) :]
        else:
            # We're using the posixpath module to mix paths and URLs conveniently.
            source_name = name if os.sep == "/" else name.replace(os.sep, "/")
            target_name = posixpath.join(posixpath.dirname(source_name), url_path)

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
        if fragment:
            transformed_url += ("?#" if "?#" in url else "#") + fragment

        # Ensure we return a string (handle mock objects in tests)
        return str(transformed_url)

    def _get_target_name(self, url, source_name):
        """
        Get the target file name from a URL and source file name
        """
        url_path, _ = urldefrag(url)

        if url_path.startswith("/"):
            # Otherwise the condition above would have returned prematurely.
            assert url_path.startswith(settings.STATIC_URL)
            target_name = url_path[len(settings.STATIC_URL) :]
        else:
            # We're using the posixpath module to mix paths and URLs conveniently.
            source_name = (
                source_name if os.sep == "/" else source_name.replace(os.sep, "/")
            )
            target_name = posixpath.join(posixpath.dirname(source_name), url_path)

        return posixpath.normpath(target_name)

    def _build_dependency_graph(self, paths, adjustable_paths):
        """
        Build a dependency graph of all files.

        Returns:
            graph: Dict mapping each file to its dependencies
            non_adjustable: Set of files that don't need processing
        """

        # Graph structure:
        # {
        #   file_name: {
        #     'dependencies': set(dependency_files),
        #     'dependents': set(files_that_depend_on_this),
        #     'needs_adjustment': bool,
        #     'url_positions': [(url, position), ...]
        #   }
        # }
        graph = defaultdict(
            lambda: {
                "dependencies": set(),
                "dependents": set(),
                "needs_adjustment": False,
                "url_positions": [],
            }
        )
        non_adjustable = set(paths.keys()) - set(adjustable_paths)

        # Initialize all files in the graph
        for name in paths:
            if name not in graph:
                graph[name] = {
                    "dependencies": set(),
                    "dependents": set(),
                    "needs_adjustment": False,
                    "url_positions": [],
                }

        source_map_patterns = {
            "*.css": re.compile(
                r"(?m)^/\*#[ \t](?-i:sourceMappingURL)=(?P<url>.*?)[ \t]*\*/$",
                re.IGNORECASE,
            ),
            "*.js": re.compile(
                r"(?m)^//# (?-i:sourceMappingURL)=(?P<url>.*?)[ \t]*$", re.IGNORECASE
            ),
        }

        for name in adjustable_paths:
            storage, path = paths[name]

            with storage.open(path) as original_file:
                try:
                    content = original_file.read().decode("utf-8")
                except UnicodeDecodeError:
                    graph[name]["needs_adjustment"] = True
                    continue

                url_positions = []
                dependencies = set()

                # Process CSS files
                if matches_patterns(path, ("*.css",)):
                    for url_name, position in extract_css_urls(content):
                        if self._should_adjust_url(url_name):
                            target = self._get_target_name(url_name, name)
                            dependencies.add(target)
                            url_positions.append((url_name, position))

                # Process JS files with module imports
                if self.support_js_module_import_aggregation and matches_patterns(
                    path, ("*.js",)
                ):
                    try:
                        urls = find_import_export_strings(
                            content,
                            should_ignore_url=lambda url: self._should_ignore_url(
                                name, url
                            ),
                        )
                    except ValueError as e:
                        message = e.args[0] if len(e.args) else ""
                        message = (
                            f"The js file '{name}' could not be processed.\n{message}"
                        )
                        raise ValueError(message)
                    for url_name, position in urls:
                        if self._should_adjust_url(url_name):
                            target = self._get_target_name(url_name, name)
                            dependencies.add(target)
                            url_positions.append((url_name, position))

                # Check for sourceMappingURL
                if "sourceMappingURL" in content:
                    for extension, pattern in source_map_patterns.items():
                        if matches_patterns(name, (extension,)):
                            for match in pattern.finditer(content):
                                url = match.group("url")
                                if self._should_adjust_url(url):
                                    target = self._get_target_name(url, name)
                                    dependencies.add(target)
                                    url_positions.append((url, match.start("url")))

                # Update graph with dependencies and URL positions
                if url_positions:
                    graph[name]["url_positions"] = url_positions
                    graph[name]["needs_adjustment"] = True

                    # Add dependencies to the graph
                    graph[name]["dependencies"].update(dependencies)

                    # Update dependents for each dependency
                    for dep in dependencies:
                        if dep in graph:
                            graph[dep]["dependents"].add(name)
                else:
                    non_adjustable.add(name)

        return graph, non_adjustable

    def _topological_sort(self, graph, non_adjustable):
        """
        Sort the files in dependency order using Kahn's algorithm.
        Files with no dependencies (or only dependencies on non-adjustable files)
        come first.

        Returns:
            List of files that have linear dpendencies in processing order
            Dict of files that have circular dependencies
        """
        result = []
        in_degree = defaultdict(int)
        circular = {}

        # Calculate in-degree for each node
        # (count dependencies not in non_adjustable)
        for node, data in graph.items():
            if node not in non_adjustable and data["needs_adjustment"]:
                for dep in data["dependencies"]:
                    if dep not in non_adjustable and dep in graph:
                        in_degree[node] += 1

        # Start with nodes that have no dependencies or only depend on
        # non-adjustable files
        queue = [
            node
            for node, data in graph.items()
            if node not in non_adjustable
            and data["needs_adjustment"]
            and in_degree[node] == 0
        ]

        while queue:
            node = queue.pop(0)
            result.append(node)

            # Remove this node from the graph (reduce in-degree of dependents)
            for dependent in graph[node]["dependents"]:
                if dependent not in non_adjustable and dependent not in result:
                    in_degree[dependent] -= 1
                    if in_degree[dependent] == 0:
                        queue.append(dependent)

        # If we still have nodes with in-degree > 0, we have cycles
        remaining = {
            node: data
            for node, data in graph.items()
            if node not in result
            and node not in non_adjustable
            and data["needs_adjustment"]
        }

        if remaining:
            # Detect and record circular dependencies
            for node in remaining:
                circular[node] = [
                    dep for dep in graph[node]["dependencies"] if dep in remaining
                ]

        return result, circular

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
                    raise ValueError(message)

            result_parts.append(transformed_url)
            last_position = position + len(url)

        # Add remaining content
        result_parts.append(content[last_position:])
        return "".join(result_parts)

    def _process_file(self, name, storage_and_path, hashed_files, graph):
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
            if name in graph and graph[name]["needs_adjustment"]:
                try:
                    if hasattr(original_file, "seek"):
                        original_file.seek(0)

                    content = original_file.read().decode("utf-8")

                    # Apply URL substitutions using stored positions
                    content = self._process_file_content(
                        name, content, graph[name]["url_positions"], hashed_files
                    )

                    # Create a content file and calculate its hash
                    content_file = ContentFile(content.encode())
                    new_hashed_name = self.hashed_name(name, content_file)

                    # Handle file saving logic
                    if hashed_file_exists and not self.keep_intermediate_files:
                        self.delete(hashed_name)
                    elif self.keep_intermediate_files and not hashed_file_exists:
                        # Save original hashed file for reference if needed
                        self._save(hashed_name, content_file)

                    if (
                        not self.exists(new_hashed_name)
                        or hashed_name != new_hashed_name
                    ):
                        if self.exists(new_hashed_name):
                            self.delete(new_hashed_name)
                        saved_name = self._save(new_hashed_name, content_file)
                        hashed_name = self.clean_name(saved_name)
                    else:
                        hashed_name = new_hashed_name

                    processed = True

                except UnicodeDecodeError as exc:
                    return name, None, exc
                except ValueError as exc:
                    exc = self._make_helpful_exception(exc, name)
                    return name, None, exc

            elif not processed and not hashed_file_exists:
                # For non-adjustable files or when processing fails,
                # just copy the file
                if hasattr(original_file, "seek"):
                    original_file.seek(0)
                processed = True
                saved_name = self._save(hashed_name, original_file)
                hashed_name = self.clean_name(saved_name)

            return name, hashed_name, processed

    def _process_with_dependency_graph(self, paths, dry_run=False, **options):
        """
        Process static files using a unified dependency graph approach.
        """
        if dry_run:
            return

        # Build the dependency graph
        adjustable_paths = [
            path for path in paths if matches_patterns(path, self._patterns)
        ]

        graph, non_adjustable = self._build_dependency_graph(paths, adjustable_paths)

        # Sort files in dependency order
        linear_deps, circular_deps = self._topological_sort(graph, non_adjustable)

        # Handle circular dependencies
        if circular_deps:
            problem_paths = ", ".join(sorted(circular_deps))
            yield problem_paths, None, RuntimeError(
                "Max post-process passes exceeded for circular dependencies."
            )
            return

        # Dictionary to store hashed file names
        hashed_files = {}

        # First process non-adjustable files and linear dependencies
        for name in list(non_adjustable) + linear_deps:
            result = self._process_file(name, paths[name], hashed_files, graph=graph)
            if result:
                name, hashed_name, processed = result
                if processed:
                    hashed_files[self.hash_key(self.clean_name(name))] = hashed_name
                    yield name, hashed_name, processed

        # Store the processed paths
        self.hashed_files.update(hashed_files)

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
            "ignore_errors": [{filename}:{url}]
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
        """
        Enhanced post_process with keep_original_files support (ticket_27929).
        """
        self.hashed_files = {}
        original_files_to_delete = []

        for name, hashed_name, processed in super().post_process(*args, **kwargs):
            yield name, hashed_name, processed
            # Track original files to delete if keep_original_files is False
            if (
                not self.keep_original_files
                and processed
                and name != hashed_name
                and self.exists(name)
            ):
                original_files_to_delete.append(name)

        if not kwargs.get("dry_run"):
            self.save_manifest()
            # Delete original files after processing is complete
            if not self.keep_original_files:
                for name in original_files_to_delete:
                    if self.exists(name):
                        self.delete(name)


class EnhancedManifestStaticFilesStorage(
    EnhancedManifestFilesMixin, StaticFilesStorage
):
    """
    Enhanced ManifestStaticFilesStorage:

    - ticket_21080: CSS lexer for better URL parsing
    - ticket_27929: keep_original_files option
    - ticket_28200: Optimized storage to avoid unnecessary file operations
    - ticket_34322: JsLex for ES module support
    - ignore_errors: List of 'file:url' errors to ignore during post-processing
    """

    def __init__(
        self,
        location=None,
        base_url=None,
        max_post_process_passes=None,
        support_js_module_import_aggregation=None,
        manifest_name=None,
        manifest_strict=None,
        keep_intermediate_files=None,
        keep_original_files=None,
        ignore_errors=None,
        *args,
        **kwargs,
    ):
        # Django 4.2/5.0 compatibility: Recover OPTIONS from STORAGES when
        # STATICFILES_STORAGE is auto-generated from STORAGES setting
        # In Django 5.1+, the deprecated STATICFILES_STORAGE setting was removed
        if django.VERSION[:2] in [(4, 2), (5, 0)]:
            self._recover_options_from_storages(kwargs)

        # Set configurable attributes as instance attributes if provided
        if max_post_process_passes is not None:
            self.max_post_process_passes = max_post_process_passes
        if support_js_module_import_aggregation is not None:
            self.support_js_module_import_aggregation = (
                support_js_module_import_aggregation
            )
        if manifest_name is not None:
            self.manifest_name = manifest_name
        if manifest_strict is not None:
            self.manifest_strict = manifest_strict
        if keep_intermediate_files is not None:
            self.keep_intermediate_files = keep_intermediate_files
        if keep_original_files is not None:
            self.keep_original_files = keep_original_files
        if ignore_errors is not None:
            self.ignore_errors = ignore_errors
        else:
            self.ignore_errors = []

        super().__init__(location, base_url, *args, **kwargs)

    def _recover_options_from_storages(self, kwargs):
        """
        Django 4.2/5.0 compatibility: When STORAGES is overridden, Django automatically
        sets STATICFILES_STORAGE to the BACKEND value, but loses the OPTIONS.
        This method recovers the OPTIONS from the original STORAGES setting.
        """
        # Check if we can detect that STATICFILES_STORAGE was auto-generated
        # from STORAGES
        # This happens when:
        # 1. STATICFILES_STORAGE points to our class
        # 2. STORAGES[staticfiles] has OPTIONS but kwargs is empty
        # 3. Either we're in a test override or STORAGES was explicitly set

        staticfiles_storage_config = settings.STORAGES.get(
            STATICFILES_STORAGE_ALIAS, {}
        )
        storage_options = staticfiles_storage_config.get("OPTIONS", {})

        # If STORAGES has OPTIONS but we didn't receive them in kwargs,
        # and STATICFILES_STORAGE points to our class, recover the options
        if (
            storage_options
            and not kwargs
            and settings.STATICFILES_STORAGE
            == (
                "django_manifeststaticfiles_enhanced.storage."
                "EnhancedManifestStaticFilesStorage"
            )
        ):

            # Add missing options to kwargs
            for option_name, option_value in storage_options.items():
                kwargs[option_name] = option_value

        # Apply kwargs options to instance attributes to bridge the gap between
        # explicit parameters and OPTIONS dict
        option_mapping = {
            "max_post_process_passes": "max_post_process_passes",
            "support_js_module_import_aggregation": (
                "support_js_module_import_aggregation"
            ),
            "manifest_name": "manifest_name",
            "manifest_strict": "manifest_strict",
            "keep_intermediate_files": "keep_intermediate_files",
            "keep_original_files": "keep_original_files",
            "ignore_errors": "ignore_errors",
        }

        for kwarg_name, attr_name in option_mapping.items():
            if kwarg_name in kwargs:
                setattr(self, attr_name, kwargs.pop(kwarg_name))
