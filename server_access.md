# Подключение к серверу

## Актуальный прод Fullbox.ru

- Сервер: `93.123.255.241`
- Пользователь: `user`
- Пароль: `AxzCrM98zK`
- SSH: `ssh user@93.123.255.241`
- Путь проекта: `/opt/fullbox`
- Виртуальное окружение: `/opt/fullbox/.venv`
- env: `/opt/fullbox/.env`
- systemd-сервис: `fullbox`
- nginx site: `/etc/nginx/sites-available/fullbox_mirror`

## Параметры приложения на сервере

Из актуального `/opt/fullbox/.env`:

```env
DJANGO_ALLOWED_HOSTS=127.0.0.1,localhost,93.123.255.241,fullbox.ru,www.fullbox.ru
DJANGO_DEBUG=False
DB_NAME=fullbox
DB_USER=fullbox
DB_PASSWORD=fullbox_db_pass
DB_HOST=127.0.0.1
DB_PORT=5432
```

## Быстрый вход

```bash
ssh user@93.123.255.241
cd /opt/fullbox
source .venv/bin/activate
python fullbox/manage.py check
```

## Проверка сервисов

```bash
sudo systemctl status fullbox
sudo systemctl status nginx
sudo systemctl status postgresql
```

## Legacy-контур

- Старый сервер: `95.163.227.182`
- Старый доступ по ключу: `~/.ssh/fullbox_root`
- Использовать только если нужна сверка, миграция или разбор старого контура.

См. также:

- `FULLBOX_RU_RUNBOOK.md`
- `MIRROR_SERVER_ACCESS.md`
- `LEGACY_SERVER_REENABLE.md`
