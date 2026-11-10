import os

DEFAULT_SITE_NAME = "OnawaBot"


def site_name():
    """
    The product name shown on the website and used by the bot.
    Set SITE_NAME on Render to change it everywhere without touching code.
    """
    name = " ".join((os.getenv("SITE_NAME") or "").split())[:40]
    return name or DEFAULT_SITE_NAME