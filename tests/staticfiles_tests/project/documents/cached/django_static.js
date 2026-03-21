// Example usage of django.static() for resolving hashed static file paths.
// During collectstatic these calls are detected so the referenced assets
// are included in the generated staticjs/django.js manifest.

const logoUrl = django.static("cached/img/relative.png");
const iconUrl = django.static('cached/img/window.png');

// These should NOT be detected (inside comments):
// const ignored = django.static("cached/img/relative.png");
/* const alsoIgnored = django.static("cached/img/window.png"); */
