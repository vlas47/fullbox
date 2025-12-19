# Подключение к серверу

- Сервер: `95.163.227.182`
- Пользователь: `root`
- SSH-ключ: приватный `~/.ssh/fullbox_root`, публичный `~/.ssh/fullbox_root.pub`
- Резервный пароль (при необходимости): `UJr4TsiVpEOVnakW`
- Путь развёрнутого проекта: `/opt/fullbox` (виртуальное окружение `/opt/fullbox/.venv`)
- База данных: PostgreSQL, БД `fullbox`, пользователь `fullbox`
- Фактический пароль на сервере (в `/opt/fullbox/.env`): `fullbox_db_pass`

## Как подключиться по ключу
1. Убедиться, что ключи на месте: `ls ~/.ssh/fullbox_root*`.
2. Подключиться: `ssh -i ~/.ssh/fullbox_root root@95.163.227.182`.

## Если нужно добавить ключ на новый клиент
1. Скопировать приватный/публичный ключи из `~/.ssh/fullbox_root*` на новый компьютер (или сгенерировать новый).
2. Добавить публичный ключ на сервер:
   - Скопировать содержимое `fullbox_root.pub`.
   - Подключиться на сервер по имеющемуся доступу и выполнить:
     ```
     mkdir -p ~/.ssh && chmod 700 ~/.ssh
     echo "<PUBLIC_KEY>" >> ~/.ssh/authorized_keys
     chmod 600 ~/.ssh/authorized_keys
     ```
3. Проверить вход: `ssh -i ~/.ssh/fullbox_root root@95.163.227.182`.

## Быстрый запуск приложения на сервере
```
ssh -i ~/.ssh/fullbox_root root@95.163.227.182
cd /opt/fullbox
source .venv/bin/activate
python fullbox/manage.py runserver 0.0.0.0:8000
```

## Подключение PostgreSQL в приложении
Переменные окружения (см. `.env.example`):
```
DJANGO_ALLOWED_HOSTS=95.163.227.182,127.0.0.1,localhost
DB_NAME=fullbox
DB_USER=fullbox
DB_PASSWORD=<Пароль_для_fullbox>
DB_HOST=127.0.0.1
DB_PORT=5432
```

## GitHub
- Репозиторий: `git@github.com:vlas47/fullbox.git`, ветка `main`.
- Ключ для пуша: `~/.ssh/id_ed25519` (добавлен в GitHub), при необходимости: `GIT_SSH_COMMAND="ssh -i ~/.ssh/id_ed25519"`.
