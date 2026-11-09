from django.conf import settings
from django.http import HttpResponse
from xml.sax.saxutils import escape


def robots_txt(request):
    sitemap_url = f"{settings.SITE_URL}/sitemap.xml"
    content = "\n".join([
        "User-agent: *",
        "Allow: /",
        "Disallow: /admin/",
        "Disallow: /dashboard/",
        "Disallow: /chatbot/webhook/",
        f"Sitemap: {sitemap_url}",
        "",
    ])
    return HttpResponse(content, content_type="text/plain; charset=utf-8")


def sitemap_xml(request):
    public_paths = (
        "/",
        "/chatbot/privacy/",
        "/chatbot/data-deletion/",
    )
    entries = "".join(
        f"<url><loc>{escape(settings.SITE_URL + path)}</loc></url>"
        for path in public_paths
    )
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"{entries}</urlset>"
    )
    return HttpResponse(xml, content_type="application/xml; charset=utf-8")
