# Fullbox на Django

- Стек: Python 3.12, Django 6.0.
- Локальное окружение: виртуальное окружение `.venv` в корне проекта.
- Проектная структура: корневая папка `fullbox/` содержит `manage.py` и пакет `fullbox/` с настройками Django.
- Зависимости фиксируются в `requirements.txt`.
- Поддерживаются две базы: по умолчанию SQLite; при наличии переменных окружения `DB_NAME/USER/PASSWORD/HOST/PORT` используется PostgreSQL.
