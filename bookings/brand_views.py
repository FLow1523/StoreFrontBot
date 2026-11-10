from pathlib import Path

from django.http import FileResponse, Http404
from django.views.decorators.http import require_safe

# Only these exact files can be requested, so nothing else on the server can be read through this view.
BRAND_DIR = Path(__file__).resolve().parent / "brand"
BRAND_FILES = {
    "favicon.ico": "image/x-icon",
    "favicon-32.png": "image/png",
    "favicon-192.png": "image/png",
    "icon-512.png": "image/png",
    "apple-touch-icon.png": "image/png",
}


@require_safe
def brand_file(request, name):
    content_type = BRAND_FILES.get(name)
    path = BRAND_DIR / name if content_type else None
    if path is None or not path.is_file():
        raise Http404("Not found")
    response = FileResponse(open(path, "rb"), content_type=content_type)
    response["Cache-Control"] = "public, max-age=86400"
    return response


def favicon(request):
    """Browsers ask for /favicon.ico on their own, on every page including the admin."""
    return brand_file(request, "favicon.ico")