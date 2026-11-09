import os

from django.conf import settings
from django.http import HttpResponse
from django.utils.html import escape

# Optional: set these in Render > Environment to personalise the pages.
BUSINESS_NAME = os.getenv("BUSINESS_NAME", "StorefrontBot")
CONTACT_EMAIL = os.getenv("PRIVACY_CONTACT_EMAIL", "contact@example.com")
LAST_UPDATED = "7 November 2026"

PAGE_STYLE = """
body { font-family: Arial, Helvetica, sans-serif; line-height: 1.6; color: #1a1a1a;
       max-width: 760px; margin: 40px auto; padding: 0 20px; }
h1 { font-size: 1.8rem; margin-bottom: 0.2rem; }
h2 { font-size: 1.2rem; margin-top: 2rem; }
p.meta { color: #666; margin-top: 0; }
"""


def render_page(request, title, description, body_html):
    canonical_url = f"{settings.SITE_URL}{request.path}"
    html = (
        "<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>{escape(title)} - {escape(BUSINESS_NAME)}</title>"
        f"<meta name=\"description\" content=\"{escape(description)}\">"
        f"<link rel=\"canonical\" href=\"{escape(canonical_url)}\">"
        f"<link rel=\"icon\" type=\"image/svg+xml\" href=\"{escape(settings.STATIC_URL)}images/favicon.svg\">"
        f"<meta property=\"og:type\" content=\"website\"><meta property=\"og:title\" content=\"{escape(title)} - {escape(BUSINESS_NAME)}\">"
        f"<meta property=\"og:description\" content=\"{escape(description)}\"><meta property=\"og:url\" content=\"{escape(canonical_url)}\">"
        f"<style>{PAGE_STYLE}</style></head><body>"
        f"<nav aria-label=\"Breadcrumb\"><a href=\"/\">Home</a> / {escape(title)}</nav>"
        f"<h1>{escape(title)}</h1>"
        f"<p class=\"meta\">Last updated: {LAST_UPDATED}</p>"
        f"{body_html}"
        "</body></html>"
    )
    return HttpResponse(html, content_type="text/html; charset=utf-8")


def privacy_policy(request):
    name = escape(BUSINESS_NAME)
    email = escape(CONTACT_EMAIL)
    body = f"""
<p>This policy explains how {name} handles personal information when you use our
WhatsApp assistant, which helps customers make bookings, check stock and get support.</p>

<h2>Information we collect</h2>
<ul>
  <li>Your WhatsApp phone number.</li>
  <li>The messages you send to the assistant.</li>
  <li>Booking details you give us: your name, preferred date, time and service.</li>
  <li>Technical records used to prevent duplicate and excessive messages, such as a
      message identifier and a keyed hash of your phone number.</li>
</ul>

<h2>How we use your information</h2>
<ul>
  <li>To reply to your messages and answer your questions.</li>
  <li>To create, manage and remind you about bookings.</li>
  <li>To protect the service from abuse and keep it reliable.</li>
</ul>

<h2>Service providers</h2>
<p>We do not sell your personal information. We use these providers to run the service:</p>
<ul>
  <li>Meta (WhatsApp Business Platform) to send and receive WhatsApp messages.</li>
  <li>Groq to generate automated replies. The text of your message is sent to it for processing.</li>
  <li>Render to host the application and its database.</li>
</ul>

<h2>How long we keep information</h2>
<ul>
  <li>Message-processing records (message identifier and hashed phone number) are deleted automatically after about 7 days.</li>
  <li>Recent conversation context is held in the server's memory only and is cleared when the service restarts.</li>
  <li>Booking records are kept until they are no longer needed or you ask us to delete them.</li>
</ul>

<h2>Your choices and rights</h2>
<p>You may ask us to access, correct or delete the information we hold about you.
See our <a href="/chatbot/data-deletion/">data deletion instructions</a> or email
<a href="mailto:{email}">{email}</a>.</p>

<h2>Security</h2>
<p>We use reasonable technical measures to protect your information, including encrypted
connections and verification of messages received from WhatsApp.</p>

<h2>Changes to this policy</h2>
<p>We may update this policy from time to time. The date at the top shows when it last changed.</p>

<h2>Contact</h2>
<p>Questions about this policy: <a href="mailto:{email}">{email}</a>.</p>
"""
    return render_page(request, "Privacy Policy", "Learn how StorefrontBot handles WhatsApp messages, booking details and personal information.", body)


def data_deletion(request):
    name = escape(BUSINESS_NAME)
    email = escape(CONTACT_EMAIL)
    body = f"""
<p>You can ask {name} to delete the personal information we hold about you at any time.</p>

<h2>How to request deletion</h2>
<ol>
  <li>Send an email to <a href="mailto:{email}">{email}</a> with the subject
      "Data deletion request".</li>
  <li>In the email, include the phone number you use on WhatsApp so we can find your records.</li>
  <li>We will confirm your request and delete your booking records and related
      information within 30 days.</li>
</ol>

<h2>What is deleted</h2>
<p>Bookings and any other records linked to your phone number. Message-processing
records are deleted automatically after about 7 days.</p>

<p>See also our <a href="/chatbot/privacy/">Privacy Policy</a>.</p>
"""
    return render_page(request, "Data Deletion Instructions", "Find out how to request deletion of personal information held by StorefrontBot.", body)
