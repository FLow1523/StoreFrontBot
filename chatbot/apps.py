import os
import sys

from django.apps import AppConfig


class ChatbotConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'chatbot'

    def ready(self):
        # Never start background work during management commands.
        if any(arg in sys.argv for arg in (
            "migrate", "makemigrations", "test", "shell", "collectstatic",
            "createsuperuser", "dumpdata", "loaddata",
        )):
            return

        # The Django dev server runs a reloader parent plus a worker child,
        # both of which call ready(). Only the child (RUN_MAIN=true) should
        # start the scheduler.
        if "runserver" in sys.argv and os.environ.get("RUN_MAIN") != "true":
            return

        # On Gunicorn/uWSGI on Render, multiple workers each call ready().
        # Require an explicit opt-in so the scheduler runs in exactly one
        # process. Set RUN_SCHEDULER=true on a single service (either a
        # single-worker web service, or a dedicated worker dyno).
        if os.environ.get("RUN_SCHEDULER", "false").lower() != "true":
            return

        from chatbot import scheduler
        scheduler.start()