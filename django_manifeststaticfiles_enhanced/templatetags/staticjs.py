from django import template
from django.conf import settings
from django.templatetags.static import static
from django.utils.safestring import mark_safe

register = template.Library()


@register.simple_tag
def include_staticjs():
    """
    Template tag to include the staticjs/django.js file with proper static URL.

    This template tag will generate a script tag with:
    1. The proper hashed URL for staticjs/django.js
    2. An ID attribute of "staticjs-static-url"
    3. A data attribute with the STATIC_URL value

    """
    static_url = getattr(settings, "STATIC_URL", "/static/")
    js_url = static("staticjs/django.js")
    script_tag = (
        f'<script src="{js_url}"'
        f' id="staticjs-static-url" data-static-url="{static_url}"></script>'
    )

    return mark_safe(script_tag)
