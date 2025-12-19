# import requests
# import oracledb
# from paramet import username, password, host, port, service_name, API_KEY, BASE_URL


# # Подключение к Oracle
# oracledb.init_oracle_client(lib_dir=None)
# dsn = f"{host}:{port}/{service_name}"

# sql = f"""select INN from fullbox.fb_agns where AGN_NAME is null """

# def get_data_db():
#     try:
#         with oracledb.connect(user=username, password=password, dsn=dsn) as connection:
#                 with connection.cursor() as cursor:
#                     print("✅ Подключение установлено. Получаем данные...")
#                     # Получаем информацию из sql
#                     cursor.execute(sql)
#                     row = cursor.fetchall()
#                     if row:
#                         for i in row:
#                             print(i[0])
#                             company_info = get_company_info(i[0])
#                             print(company_info)
#                             if company_info:
#                                 for suggestion in company_info['suggestions']:
#                                     company_data = suggestion['data']

#                                     # print(company_data['name']['full_with_opf'])
#                                     # print(company_data['inn'])
#                                     # print(company_data['ogrn'])
#                                     # print(company_data['kpp'])
#                                     # print(company_data['management']['name'])
#                                     # print(company_data['management']['post'])
#                                     # print(company_data['address']['value'])
#                                     # print(company_data['state']['registration_date'])
#                                     # print(company_data['phones'])
#                                     try:
#                                         cursor.execute(f"""UPDATE FULLBOX.FB_AGNS
#                                                         SET AGN_NAME= '{company_data['name']['full_with_opf']}',  
#                                                         KPP= {company_data['kpp']}, 
#                                                         ADRES= '{company_data['address']['value']}',
#                                                         FIO_AGN = '{company_data['management']['name']}'
#                                                         where INN = {i[0]} """)
#                                         connection.commit()
#                                         print(1)
#                                     except:
#                                         cursor.execute(f"""UPDATE FULLBOX.FB_AGNS
#                                                         SET AGN_NAME= '{company_data['name']['full_with_opf']}',
#                                                         ADRES= '{company_data['address']['value']}'
#                                                         where INN = {i[0]} """)
#                                         connection.commit()
#                                         print(2)
#                             else:
#                                 print("Не удалось получить информацию о компании.")
#                     else:
#                         raise Exception("Не удалось получить данные по предложению")
#     except Exception as e:
#         print(e)
    



# def get_company_info(inn):
#     headers = {
#         "Authorization": f"Token {API_KEY}",
#         "Content-Type": "application/json",
#     }
#     data = {
#         "query": inn
#     }
#     response = requests.post(BASE_URL, headers=headers, json=data)
#     if response.status_code == 200:
#         return response.json()
#     else:
#         return None

# if __name__ == "__main__":
#     get_data_db()







import requests
import oracledb
from paramet import username, password, host, port, service_name, API_KEY, BASE_URL


# ==============================
# 🔧 Подключение к Oracle
# ==============================
oracledb.init_oracle_client(lib_dir=None)
dsn = f"{host}:{port}/{service_name}"
sql = """SELECT INN FROM fullbox.fb_agns WHERE AGN_NAME IS NULL"""


# ==============================
# 📡 Запрос к API
# ==============================
def get_company_info(inn):
    """Получение данных о компании по ИНН через API"""
    headers = {
        "Authorization": f"Token {API_KEY}",
        "Content-Type": "application/json",
    }
    data = {"query": inn}

    try:
        response = requests.post(BASE_URL, headers=headers, json=data, timeout=10)
        if response.status_code == 200:
            return response.json()
        else:
            print(f"❌ Ошибка API {response.status_code}: {response.text}")
            return None
    except Exception as e:
        print(f"❌ Ошибка при запросе API ({inn}): {e}")
        return None


# ==============================
# 🧩 Обновление данных в Oracle
# ==============================
def update_company_data(cursor, connection, inn, data):
    """Обновление данных в таблице FULLBOX.FB_AGNS"""

    if not data or not isinstance(data, dict):
        print(f"⚠️ Пропуск ИНН {inn}: data отсутствует или не является словарём -> {data}")
        return

    # Безопасно извлекаем поля
    company_type = data.get("type")  # "LEGAL" или "INDIVIDUAL"
    name_data = data.get("name", {}) or {}
    address_data = data.get("address", {}) or {}
    management_data = data.get("management", {}) or {}

    # Название: предпочтительно короткое ("ИП Иванов И.И."), иначе полное
    agn_name = name_data.get("short_with_opf") or name_data.get("full_with_opf") or ""

    # Адрес (для ИП может отсутствовать)
    adres = address_data.get("value") or ""

    # Для ООО — KPP и ФИО руководителя
    kpp = data.get("kpp")
    fio_agn = management_data.get("name")

    try:
        if kpp and fio_agn:
            # Для ООО
            cursor.execute("""
                UPDATE FULLBOX.FB_AGNS
                SET AGN_NAME = :agn_name,
                    KPP = :kpp,
                    ADRES = :adres,
                    FIO_AGN = :fio_agn
                WHERE INN = :inn
            """, agn_name=agn_name, kpp=kpp, adres=adres, fio_agn=fio_agn, inn=inn)
            print(f"✅ Обновлено (ООО): {inn} — {agn_name}")

        else:
            # Для ИП
            cursor.execute("""
                UPDATE FULLBOX.FB_AGNS
                SET AGN_NAME = :agn_name,
                    ADRES = :adres
                WHERE INN = :inn
            """, agn_name=agn_name, adres=adres, inn=inn)
            print(f"✅ Обновлено (ИП): {inn} — {agn_name}")

        connection.commit()

    except Exception as e:
        print(f"❌ Ошибка при обновлении {inn}: {e}")


# ==============================
# ⚙️ Основной процесс
# ==============================
def get_data_db():
    """Основная функция: получение ИНН и обновление данных"""
    try:
        with oracledb.connect(user=username, password=password, dsn=dsn) as connection:
            with connection.cursor() as cursor:
                print("✅ Подключен к Oracle. Получаем данные...")

                cursor.execute(sql)
                rows = cursor.fetchall()

                if not rows:
                    print("⚠️ Нет записей для обновления.")
                    return

                for row in rows:
                    inn = row[0]
                    print(f"\n🔹 Обработка ИНН: {inn}")

                    company_info = get_company_info(inn)
                    if not company_info:
                        print(f"❌ Не удалось получить данные для ИНН {inn} (пустой ответ API)")
                        continue

                    suggestions = company_info.get("suggestions")
                    if not suggestions:
                        print(f"⚠️ Нет предложений в ответе API для ИНН {inn}")
                        continue

                    suggestion = suggestions[0]
                    data = suggestion.get("data")

                    if not data or not isinstance(data, dict):
                        print(f"⚠️ Неверная структура данных для ИНН {inn}: {data}")
                        continue

                    # Отладочная печать — можно закомментировать
                    # print(f"DEBUG: {data}")

                    update_company_data(cursor, connection, inn, data)

    except Exception as e:
        print(f"❌ Ошибка при работе с БД: {e}")


# ==============================
# 🚀 Точка входа
# ==============================
if __name__ == "__main__":
    get_data_db()
