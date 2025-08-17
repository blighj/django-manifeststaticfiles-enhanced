# Django ManifestStaticFiles Enhanced

Enhanced ManifestStaticFilesStorage for Django.

## AI tools were used for creation of this package
- **Tool(s) used:** Claude Code
- **What it was used for:** Used for building initial versions of all features and for initial versions of original tests.

## Features

This package includes several improvements to Django's `ManifestStaticFilesStorage`:

- **[ticket_27929](https://code.djangoproject.com/ticket/27929)**: `keep_original_files` option to control whether original files are deleted after hashing
- **[ticket_21080](https://code.djangoproject.com/ticket/21080)**: CSS lexer for better URL parsing in CSS files
- **[ticket_34322](https://code.djangoproject.com/ticket/34322)**: JsLex for ES module support in JavaScript files
- **[ticket_28200](https://code.djangoproject.com/ticket/28200)**: Optimized storage to avoid unnecessary file operations for unchanged files
- **[ticket_26329](https://code.djangoproject.com/ticket/26329)**: Ensure production errors are rasied in development too

## Compatibility

- **Django**: 4.2, 5.0, 5.1, 5.2
- **Python**: 3.9, 3.10, 3.11, 3.12, 3.13

## Installation

```bash
pip install django-manifeststaticfiles-enhanced
```

## Usage

### Basic Usage

Replace Django's default `ManifestStaticFilesStorage` with the enhanced version using the `STORAGES` setting:

```python
# settings.py
STORAGES = {
    "staticfiles": {
        "BACKEND": "django_manifeststaticfiles_enhanced.storage.EnhancedManifestStaticFilesStorage",
    },
}
```

### Configuration Options

#### keep_original_files ([ticket_27929](https://code.djangoproject.com/ticket/27929))

Control whether original files are kept after hashing:

```python
# settings.py - Keep original files (default)
STORAGES = {
    "staticfiles": {
        "BACKEND": "django_manifeststaticfiles_enhanced.storage.EnhancedManifestStaticFilesStorage",
        "OPTIONS": {
            "keep_original_files": True,  # Default
        },
    },
}

# Or delete original files to save space
STORAGES = {
    "staticfiles": {
        "BACKEND": "django_manifeststaticfiles_enhanced.storage.EnhancedManifestStaticFilesStorage",
        "OPTIONS": {
            "keep_original_files": False,
        },
    },
}
```

#### JavaScript Module Support ([ticket_34322](https://code.djangoproject.com/ticket/34322))

Enable ES module import/export processing:

```python
# settings.py
STORAGES = {
    "staticfiles": {
        "BACKEND": "django_manifeststaticfiles_enhanced.storage.EnhancedManifestStaticFilesStorage",
        "OPTIONS": {
            "support_js_module_import_aggregation": True,
        },
    },
}
```

#### Easy access to existing options
Disable [manifest_strict](https://docs.djangoproject.com/en/5.2/ref/contrib/staticfiles/#django.contrib.staticfiles.storage.ManifestStaticFilesStorage.manifest_strict)
```python
# settings.py
STORAGES = {
    "staticfiles": {
        "BACKEND": "django_manifeststaticfiles_enhanced.storage.EnhancedManifestStaticFilesStorage",
        "OPTIONS": {
            "manifest_strict": False,
        },
    },
}
```

Also available:
 - manifest_name: change the name of the staticfiles.json file

## Feature Details

### CSS Processing Improvements ([ticket_21080](https://code.djangoproject.com/ticket/21080))

CSS URL processing uses a proper lexer instead of regex, providing:

- Ignores url's in comments
- More reliable URL extraction
- Wider @import support

### File Operation Optimization ([ticket_28200](https://code.djangoproject.com/ticket/28200))

Reduces unnecessary file operations during `collectstatic`:

- Avoids recreating files that haven't changed
- Checks file existence before deletion

### JavaScript Module Support ([ticket_34322](https://code.djangoproject.com/ticket/34322))

JS import/export processing uses a proper lexer instead of regex, providing:

- Covers ES6 import/export statements and dynamic imports
- Ignores statements in comments and strings
- Supports assert statements in imports, `import sheet from './styles.css' assert { type: 'css' };`

Example JavaScript that gets processed:

```javascript
// Before processing
import { Component } from './component.js';
export { utils } from './utils.js';

// After processing (with hashing)
import { Component } from './component.abc123.js';
export { utils } from './utils.def456.js';
```

### Option to not move the origianl asset to your static folder ([ticket_27929](https://code.djangoproject.com/ticket/27929))

Control file cleanup behavior:

```python
# Keep original files (default)
keep_original_files = True
# Results in: style.css + style.abc123.css

# Delete original files
keep_original_files = False  
# Results in: style.abc123.css only
```

### Ignoring specific errors

Ignore specific errors during post-processing with the `ignore_errors` option. This is useful when you have third-party libraries that reference non-existent files or use dynamic path construction that can't be properly parsed.

```python
# settings.py
STORAGES = {
    "staticfiles": {
        "BACKEND": "django_manifeststaticfiles_enhanced.storage.EnhancedManifestStaticFilesStorage",
        "OPTIONS": {
            "ignore_errors": [
                # Format: "file_pattern:missing_url_pattern"
                "vendor/bootstrap/*.css:missing-font.woff",  # Ignore missing font in bootstrap CSS
                "vendor/es/*.js:*",  # Ignore all missing missing references in vendors ES version
                "*/*.css:../img/background.png"  # Ignore specific missing image in all CSS files
            ],
        },
    },
}
```

Patterns support wildcard matching with `*` to match any number of characters.

### Catching common errors between development and production

The `{% static %}` tag does not use the manifest file in development, when `DEBUG=True`, instead it defaults to `StaticFilesStorage`. This can lead to errors that only show up in staging/production enviornments, because some static paths will work for `StaticFilesStorage` but not be valid paths in the manifest file and cause errors when `DEBUG=False`.  This class adds extra validation when `DEBUG=True` so that the errors can be caught earlier.
This package also includes a special storage class, `TestingManifestStaticFilesStorage`, for testing that uses `StaticFilesStorage` with the validation checks.

```python
# settings.py for testing
STORAGES = {
    "staticfiles": {
        "BACKEND": "django_manifeststaticfiles_enhanced.storage.TestingManifestStaticFilesStorage",
    },
}
```

The `TestingManifestStaticFilesStorage` includes the `DebugValidationMixin` which performs the following validations:

1. Rejects paths that start with `/` (e.g., `/static/file.css`)
2. Rejects paths that use backslashes (e.g., `path\to\file.css`)
3. Checks if the file actually exists in your static files
4. Detects case sensitivity issues that might work in development but fail in production

## Running Tests

```bash
# Install development dependencies
pip install -e .[dev]

# Run tests
python tests/runtests.py

# Or run specific test modules
python tests/runtests.py staticfiles_tests.test_storage
```

## Migration from Django's ManifestStaticFilesStorage

This package is designed as a drop-in replacement:

1. Install the package
2. Update your `STORAGES` setting
3. Run `python manage.py collectstatic` as usual

All existing functionality remains the same, with additional features available through configuration options.

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

## License

This project is licensed under the BSD 3-Clause License - the same license as Django.

## Changelog

### 0.6.0

- Surface exceptions with `{% static %}` tag when `DEBUG=True`
- Added TestingManifestStaticFilesStorage for use in tests to Surface exceptions when `DEBUG=False`

### 0.5.0

- Made support of js import/export statements opt in

### 0.4.0

- Added handling of circular dependencies in js/css files.

### 0.3.0

- Added `ignore_errors` option to allow ignoring specific file reference errors during post-processing
- Improved error handling to continue processing when errors are explicitly ignored

### 0.2.0

- Performence improvements
- Improved exception messages
- Fixed issue with js dynamic imports that include template literals
- Improved handling of soucerMapURLs

### 0.1.0 (Initial Release)

- Includes `keep_original_files` option ([ticket_27929](https://code.djangoproject.com/ticket/27929))
- Includes CSS lexer improvements ([ticket_21080](https://code.djangoproject.com/ticket/21080))
- Includes file operation optimizations ([ticket_28200](https://code.djangoproject.com/ticket/28200))
- Includes JavaScript module support ([ticket_34322](https://code.djangoproject.com/ticket/34322))
- Added comprehensive test suite
- Support for Django 4.2+
