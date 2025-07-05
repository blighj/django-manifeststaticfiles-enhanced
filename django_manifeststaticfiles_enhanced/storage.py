import os
import posixpath
import re
from urllib.parse import unquote, urldefrag

from django.conf import settings
from django.contrib.staticfiles.storage import (
    HashedFilesMixin,
    ManifestFilesMixin,
    StaticFilesStorage,
)
from django.contrib.staticfiles.utils import matches_patterns
from django.core.files.base import ContentFile

# Import our inlined jslex functionality
from django_manifeststaticfiles_enhanced.jslex import (
    extract_css_urls,
    find_import_export_strings,
)


class EnhancedHashedFilesMixin(HashedFilesMixin):
    support_js_module_import_aggregation = True
    adjust_functions = {
        "*.js": ("_process_js_modules", "_process_sourcemapping_regexs"),
        "*.css": ("_process_css_urls", "_process_sourcemapping_regexs"),
    }

    patterns = (
        (
            "*.css",
            (
                (
                    (
                        r"(?m)^(?P<matched>/\*#[ \t]"
                        r"(?-i:sourceMappingURL)=(?P<url>.*)[ \t]*\*/)$"
                    ),
                    "/*# sourceMappingURL=%(url)s */",
                ),
            ),
        ),
        (
            "*.js",
            (
                (
                    r"(?m)^(?P<matched>//# (?-i:sourceMappingURL)=(?P<url>.*))$",
                    "//# sourceMappingURL=%(url)s",
                ),
            ),
        ),
    )

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
            # Otherwise the condition above would have returned prematurely.
            assert url_path.startswith(settings.STATIC_URL)
            target_name = url_path.removeprefix(settings.STATIC_URL)
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

    def url_converter(self, name, hashed_files, template=None):
        """
        Return the custom URL converter for the given file name.
        """
        if template is None:
            template = self.default_template

        def converter(matchobj):
            """
            Convert the matched URL to a normalized and hashed URL.
            """
            matches = matchobj.groupdict()
            matched = matches["matched"]
            url = matches["url"]

            if not self._should_adjust_url(url):
                return matched

            try:
                transformed_url = self._adjust_url(url, name, hashed_files)
                matches["url"] = unquote(transformed_url)
                return template % matches
            except ValueError:
                # Return original if we can't process
                return matched

        return converter

    def _process_css_urls(self, name, content, hashed_files):
        """
        Process CSS content using the CSS lexer (ticket_21080).
        """
        search_content = content.lower()
        complex_adjustments = "url" in search_content or "import" in search_content
        if not complex_adjustments:
            return content
        result_parts = []
        last_position = 0

        url_matches = extract_css_urls(content)

        for url_name, position in url_matches:
            # Add content before this URL
            result_parts.append(content[last_position:position])

            if self._should_adjust_url(url_name):
                transformed_url = self._adjust_url(url_name, name, hashed_files)
                result_parts.append(transformed_url)
            else:
                result_parts.append(url_name)

            last_position = position + len(url_name)

        # Add remaining content
        result_parts.append(content[last_position:])
        return "".join(result_parts)

    def _process_js_modules(self, name, content, hashed_files):
        """Process JavaScript import/export statements."""
        if not self.support_js_module_import_aggregation:
            return content
        complex_adjustments = "import" in content or (
            "export" in content and "from" in content
        )

        if not complex_adjustments:
            return content

        import_matches = find_import_export_strings(content)

        if not import_matches:
            return content

        result_parts = []
        last_position = 0

        for import_name, position in import_matches:
            if self._should_adjust_url(import_name):
                # Add content before this import
                result_parts.append(content[last_position:position])

                # Process the import
                replacement = self._adjust_url(import_name, name, hashed_files)
                result_parts.append(replacement)
                # Update position tracker
                last_position = position + len(import_name)

        # Add remaining content
        result_parts.append(content[last_position:])
        return "".join(result_parts)

    def _process_sourcemapping_regexs(self, name, content, hashed_files):
        if "sourceMappingURL" not in content:
            return content

        for extension, patterns in self._patterns.items():
            if matches_patterns(name, (extension,)):
                for pattern, template in patterns:
                    converter = self.url_converter(name, hashed_files, template)
                    content = pattern.sub(converter, content)
        return content

    def _post_process(self, paths, adjustable_paths, hashed_files):
        """
        Enhanced _post_process with optimization from ticket_28200.
        """

        def path_level(name):
            return len(name.split(os.sep))

        for name in sorted(paths, key=path_level, reverse=True):
            substitutions = True
            storage, path = paths[name]
            with storage.open(path) as original_file:
                cleaned_name = self.clean_name(name)
                hash_key = self.hash_key(cleaned_name)

                if hash_key not in hashed_files:
                    hashed_name = self.hashed_name(name, original_file)
                else:
                    hashed_name = hashed_files[hash_key]

                if hasattr(original_file, "seek"):
                    original_file.seek(0)

                hashed_file_exists = self.exists(hashed_name)
                processed = False

                if name in adjustable_paths:
                    old_hashed_name = hashed_name
                    try:
                        content = original_file.read().decode("utf-8")
                    except UnicodeDecodeError as exc:
                        yield name, None, exc, False
                        continue

                    for extension, function_names in self.adjust_functions.items():
                        if matches_patterns(path, (extension,)):
                            for function_name in function_names:
                                function = getattr(self, function_name)
                                try:
                                    content = function(name, content, hashed_files)
                                except ValueError as exc:
                                    yield name, None, exc, False

                    content_file = ContentFile(content.encode())

                    # Optimization: only recreate if file doesn't exist or hash changed
                    new_hashed_name = self.hashed_name(name, content_file)

                    # Handle intermediate files - delete existing if not keeping them
                    if hashed_file_exists and not self.keep_intermediate_files:
                        self.delete(hashed_name)
                    elif self.keep_intermediate_files and not hashed_file_exists:
                        # Save intermediate file for reference
                        self._save(hashed_name, content_file)

                    # Only save if file doesn't exist or content changed
                    if (
                        not self.exists(new_hashed_name)
                        or old_hashed_name != new_hashed_name
                    ):
                        if self.exists(new_hashed_name):
                            self.delete(new_hashed_name)
                        saved_name = self._save(new_hashed_name, content_file)
                        hashed_name = self.clean_name(saved_name)
                    else:
                        hashed_name = new_hashed_name

                    if old_hashed_name == hashed_name:
                        substitutions = False
                    processed = True

                if not processed:
                    if not hashed_file_exists:
                        processed = True
                        saved_name = self._save(hashed_name, original_file)
                        hashed_name = self.clean_name(saved_name)

                hashed_files[hash_key] = hashed_name
                yield name, hashed_name, processed, substitutions


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
        *args,
        **kwargs,
    ):
        # Handle options from both direct parameters and Django's OPTIONS dict
        max_post_process_passes = (
            max_post_process_passes
            if max_post_process_passes is not None
            else kwargs.pop("max_post_process_passes", None)
        )
        support_js_module_import_aggregation = (
            support_js_module_import_aggregation
            if support_js_module_import_aggregation is not None
            else kwargs.pop("support_js_module_import_aggregation", None)
        )
        manifest_name = (
            manifest_name
            if manifest_name is not None
            else kwargs.pop("manifest_name", None)
        )
        manifest_strict = (
            manifest_strict
            if manifest_strict is not None
            else kwargs.pop("manifest_strict", None)
        )
        keep_intermediate_files = (
            keep_intermediate_files
            if keep_intermediate_files is not None
            else kwargs.pop("keep_intermediate_files", None)
        )
        keep_original_files = (
            keep_original_files
            if keep_original_files is not None
            else kwargs.pop("keep_original_files", None)
        )

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

        super().__init__(location, base_url, *args, **kwargs)
