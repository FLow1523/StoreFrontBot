web: python manage.py migrate && python manage.py collectstatic --noinput && gunicorn core.wsgi

web: python manage.py migrate && python manage.py createsuperuser --noinput || true && python manage.py collectstatic --noinput && gunicorn core.wsgi