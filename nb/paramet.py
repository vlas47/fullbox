# Параметры подключения
username = ""              # ← имя пользователя в Oracle
password = ""             
host = "localhost"                  # ← адрес сервера Oracle
port = 1521                             # ← порт (обычно 1521)
service_name = "xepdb1"               # ← имя сервиса или базы

# Параметры для подключения к DaData
API_KEY = ""
BASE_URL = "https://suggestions.dadata.ru/suggestions/api/4_1/rs/findById/party"


dsn = f"{host}:{port}/{service_name}"