# import oracledb
# from paramet import username, password, host, port, service_name
# import os
# import requests
# import json

# def sync_wb(paramI):
#     url = 'https://content-api.wildberries.ru/content/v2/get/cards/list'
                
#     body = {
#         "settings": {                      
#             "cursor": {
#             "limit": 100
#             },
#             "filter": {
#             "withPhoto": -1
#             }
#         }
#         }

#     params = {
#         'limit': 100,  # Количество карточек на странице
#     }

#     param = paramI

#     sql_tovar = f"""select ARTIKUL	
#                     from fullbox.FB_TOVAR_LIST
#                     where agn_id = :param"""

#     sql_k = f"""select MARCET_KEY
#                 from fullbox.FB_AGNS_MARKET_ARTIKU
#                 where MARKET_ID = 2
#                 and AGN_ID = :param"""
#     # Подключение к Oracle
#     oracledb.init_oracle_client(lib_dir=None)
#     dsn = f"{host}:{port}/{service_name}"

#     # sql_ins = """insert into FULLBOX.FB_TOVAR_LIST (NAME, NAME_PRINT, ARTIKUL, AGN_ID, TSIZE, MADE_IN, IMG) VALUES ()"""

#     try:
#         with oracledb.connect(user=username, password=password, dsn=dsn) as connection:
#             with connection.cursor() as cursor:
#                 print("✅ Подключение установлено. Получаем данные...")
#                 # Записывем данные
#                 cursor.execute(sql_k, {"param": param})
#                 data = cursor.fetchall()
#                 API_KEY = data[0][0]
#                 ################################
#                 cursor.execute(sql_tovar, {"param": param})
#                 data_art = cursor.fetchall()
#                 # Преобразуем в плоский список строк
#                 array = [item[0] for item in data_art]
#                 print(data_art)
#                 ################################
#                 headers = {
#                     'Authorization': f'{API_KEY}',
#                     "Content-Type" : "application/json"
#                 }
                
#                 response = requests.post(url, headers=headers, data=json.dumps(body))
                
#                 # Получение и обработка данных
#                 if response.status_code == 200:
#                     data = response.json()
#                     # print(data)
#                     for product in data['cards']:
#                         # Парсинг JSON
#                         data = product

#                         # Пример извлечения данных
#                         product_info = {
#                             "Название": data["title"],
#                             "Бренд": data["brand"],
#                             "Артикул": data["vendorCode"],
#                             # "Описание": data["description"],
#                             "Категория": data["subjectName"],
#                             "Габариты": f"{data['dimensions']['width']}×{data['dimensions']['height']}×{data['dimensions']['length']} см, вес: {data['dimensions']['weightBrutto']} кг",
#                             "Характеристики": {item["name"]: ", ".join(item["value"]) if isinstance(item["value"], list) else item["value"] 
#                                             for item in data["characteristics"]},
#                             "Фото": [photo["big"] for photo in data["photos"]],
#                             "Дата создания": data["createdAt"],
#                             "Дата обновления": data["updatedAt"]
#                         }

#                         # Вывод результата
#                         # print("Основная информация о товаре:")
#                         # print(f"Название: {product_info['Название']}")
#                         # print(f"Бренд: {product_info['Бренд']}")
#                         # print(f"Артикул: {product_info['Артикул']}")
#                         # print(f"Категория: {product_info['Категория']}")
#                         # print(f"Габариты: {product_info['Габариты']}\n")

#                         # print("Характеристики:")
#                         # for name, value in product_info["Характеристики"].items():
#                         #     print(f"- {name}: {value}")

#                         # print("\nСсылки на фото:")
#                         # for i, photo_url in enumerate(product_info["Фото"], 1):
#                         #     print(f"{i}. {photo_url}")
#                         # print(product_info["Фото"][0])
#                         # print(f"\nДата создания: {product_info['Дата создания']}")
#                         # print(f"Дата обновления: {product_info['Дата обновления']}")



#                         char_dict = {char['name']: char['value'] for char in product.get('characteristics', [])}

#                         def get_char_value(name):
#                             val = char_dict.get(name)
#                             if isinstance(val, list):
#                                 return val[0]
#                             else:
#                                 return 0
#                             return val

#                         тип_товара = product.get('subjectName')
#                         вес = product.get('dimensions', {}).get('weightBrutto')
#                         объем = get_char_value('Объем (мл)')
#                         длина = product.get('dimensions', {}).get('length')
#                         ширина = product.get('dimensions', {}).get('width')
#                         высота = product.get('dimensions', {}).get('height')
#                         пол = get_char_value('Пол')
#                         сезон = get_char_value('Сезон')
#                         предмет = product.get('title')
#                         состав = get_char_value('Состав')
#                         категория_товара = product.get('subjectName')

#                         print(тип_товара, вес, объем, длина, ширина, высота, пол, сезон, предмет, состав, категория_товара)
#                         print('-' * 30)




#                         if product_info['Артикул'] in array:
#                             print(f'''Карточка уже создана {product_info['Артикул']}''')
#                         else:
#                             try:
#                                 cursor.execute(f"""insert into FULLBOX.FB_TOVAR_LIST (NAME, NAME_PRINT, ARTIKUL, AGN_ID, TSIZE, MADE_IN, IMG, COLOR_NAME, BRAND, STOR_UNIT_ID,   TYPE_TOVAR, WEIGHT, VOLUME, LENGTH, WIDTH, HEIGHT, GENDER, SEASON, DOP_ITEM_NAME, COMPOSITION, MARKET_TYPE, TOVAR_CATEGORY) 
#                                                                             VALUES ('{product_info['Название']}',
#                                                                                     '{product_info['Название']}', 
#                                                                                     '{product_info['Артикул']}', 
#                                                                                     '{param}',
#                                                                                     '{product_info['Габариты']}', 
#                                                                                     '{product_info["Характеристики"]['Страна производства']}', 
#                                                                                     '{product_info["Фото"][0]}',
#                                                                                     '{product_info["Характеристики"]['Цвет']}',
#                                                                                     '{product_info["Бренд"]}',
#                                                                                     1
#                                                                                     ,'{тип_товара}'
#                                                                                     ,{вес}
#                                                                                     ,{объем}
#                                                                                     ,{длина}
#                                                                                     ,{ширина}
#                                                                                     ,{высота}
#                                                                                     ,'{пол}'
#                                                                                     ,'{сезон}'
#                                                                                     ,'{предмет}'
#                                                                                     ,'{состав}'
#                                                                                     ,2
#                                                                                     ,'{категория_товара}'
#                                                                                     )""")
                                

#                                 try:
#                                     for dt in data["sizes"]:
#                                         print(dt["techSize"])
#                                         print(dt["wbSize"])
#                                         for dt1 in dt["skus"]:
#                                             print(dt1)
#                                             cursor.execute(f"""insert into fullbox.FB_TOVAR_LIST_SCHK (pid, SCHK, TECHSIZE, WBSIZE) 
#                                                                 select max(id), '{dt1}', '{dt["techSize"]}', '{dt["wbSize"]}'
#                                                                 from fullbox.FB_TOVAR_LIST""")
                            
#                                 except Exception as e:
#                                     print(f"Ошибка при синхранизации: {e}")

#                                 connection.commit()
#                             except Exception as e:
#                                 print(f"Ошибка при синхранизации: {e}")
#                                 # cursor.execute(f"""insert into FULLBOX.FB_TOVAR_LIST (NAME, NAME_PRINT, ARTIKUL, AGN_ID, TSIZE, MADE_IN, IMG, COLOR_NAME, BRAND) 
#                                 #                                             VALUES ('{product_info['Название']}',
#                                 #                                                     '{product_info['Название']}', 
#                                 #                                                     '{product_info['Артикул']}', 
#                                 #                                                     '{param}',
#                                 #                                                     '{product_info['Габариты']}',  
#                                 #                                                     '{product_info["Фото"][0]}',
#                                 #                                                     '{product_info["Бренд"]}')""")
#                                 # connection.commit()

#                 else:
#                     print(f"Ошибка {response.status_code}: {response.text}")

                
#     except oracledb.Error as e:
#         print(f"""❌ Ошибка {e}""")




import oracledb
from paramet import username, password, host, port, service_name
import os
import requests
import json
import time


def get_all_cards(api_key):
    url = 'https://content-api.wildberries.ru/content/v2/get/cards/list'
    headers = {
        'Authorization': api_key,
        "Content-Type": "application/json"
    }

    all_cards = []
    limit = 100
    updated_at = None
    nm_id = None

    while True:
        cursor = {"limit": limit}
        if updated_at and nm_id:
            cursor["updatedAt"] = updated_at
            cursor["nmID"] = nm_id

        body = {
            "settings": {
                "cursor": cursor,
                "filter": {
                    "withPhoto": -1
                }
            }
        }

        response = requests.post(url, headers=headers, data=json.dumps(body))
        if response.status_code != 200:
            print(f"Ошибка {response.status_code}: {response.text}")
            break

        data = response.json()
        cards = data.get("cards", [])
        all_cards.extend(cards)

        if not cards or len(cards) < limit:
            break

        last_card = cards[-1]
        updated_at = last_card.get("updatedAt")
        nm_id = last_card.get("nmID")
        time.sleep(0.3)

    return all_cards


def sync_wb(paramI):
    sql_tovar = """SELECT ARTIKUL FROM fullbox.FB_TOVAR_LIST WHERE agn_id = :param"""
    sql_k = """SELECT MARCET_KEY FROM fullbox.FB_AGNS_MARKET_ARTIKU WHERE MARKET_ID = 2 AND AGN_ID = :param"""

    oracledb.init_oracle_client(lib_dir=None)
    dsn = f"{host}:{port}/{service_name}"
    param = paramI

    try:
        with oracledb.connect(user=username, password=password, dsn=dsn) as connection:
            with connection.cursor() as cursor:
                print("✅ Подключение установлено. Получаем данные...")

                cursor.execute(sql_k, {"param": param})
                API_KEY = cursor.fetchone()[0]

                cursor.execute(sql_tovar, {"param": param})
                existing_articles = [row[0] for row in cursor.fetchall()]

                print("📦 Загружаем все карточки Wildberries...")
                all_cards = get_all_cards(API_KEY)
                print(f"✅ Загружено карточек: {len(all_cards)}")

                for product in all_cards:
                    char_dict = {char['name']: char['value'] for char in product.get('characteristics', [])}

                    def get_char_value(name):
                        val = char_dict.get(name)
                        if isinstance(val, list):
                            return val[0]
                        return val or ""

                    try:
                        product_info = {
                            "Название": product.get("title"),
                            "Бренд": product.get("brand"),
                            "Артикул": product.get("vendorCode"),
                            "Категория": product.get("subjectName"),
                            "Габариты": f"{product['dimensions']['width']}×{product['dimensions']['height']}×{product['dimensions']['length']} см, вес: {product['dimensions']['weightBrutto']} кг",
                            "Характеристики": char_dict,
                            "Фото": [p["big"] for p in product.get("photos", [])],
                            "Дата создания": product.get("createdAt"),
                            "Дата обновления": product.get("updatedAt")
                        }

                        if product_info['Артикул'] in existing_articles:
                            print(f"🔄 Карточка уже создана: {product_info['Артикул']}")
                            continue

                        тип_товара = product_info['Категория']
                        вес = product['dimensions']['weightBrutto']
                        объем = get_char_value('Объем (мл)')
                        длина = product['dimensions']['length']
                        ширина = product['dimensions']['width']
                        высота = product['dimensions']['height']
                        пол = get_char_value('Пол')
                        сезон = get_char_value('Сезон')
                        предмет = product_info['Название']
                        состав = get_char_value('Состав')

                        cursor.execute(f"""
                            INSERT INTO FULLBOX.FB_TOVAR_LIST 
                            (NAME, NAME_PRINT, ARTIKUL, AGN_ID, TSIZE, MADE_IN, IMG, COLOR_NAME, BRAND, STOR_UNIT_ID, TYPE_TOVAR, 
                             WEIGHT, VOLUME, LENGTH, WIDTH, HEIGHT, GENDER, SEASON, DOP_ITEM_NAME, COMPOSITION, MARKET_TYPE, TOVAR_CATEGORY)
                            VALUES (
                                :name, :name_print, :artikul, :agn_id, :tsize, :made_in, :img, :color_name, :brand, 1,
                                :type_tovar, :weight, :volume, :length, :width, :height, :gender, :season,
                                :dop_item_name, :composition, 2, :tovar_category
                            )""", {
                                "name": предмет,
                                "name_print": предмет,
                                "artikul": product_info['Артикул'],
                                "agn_id": param,
                                "tsize": product_info['Габариты'],
                                "made_in": get_char_value('Страна производства'),
                                "img": product_info["Фото"][0] if product_info["Фото"] else "",
                                "color_name": get_char_value('Цвет'),
                                "brand": product_info['Бренд'],
                                "type_tovar": тип_товара,
                                "weight": вес,
                                "volume": объем,
                                "length": длина,
                                "width": ширина,
                                "height": высота,
                                "gender": пол,
                                "season": сезон,
                                "dop_item_name": предмет,
                                "composition": состав,
                                "tovar_category": тип_товара
                            })

                        for size in product.get("sizes", []):
                            tech = size.get("techSize")
                            wb = size.get("wbSize")
                            for sku in size.get("skus", []):
                                cursor.execute(f"""
                                    INSERT INTO fullbox.FB_TOVAR_LIST_SCHK (pid, SCHK, TECHSIZE, WBSIZE)
                                    SELECT MAX(id), :sku, :tech, :wb FROM fullbox.FB_TOVAR_LIST
                                """, {
                                    "sku": sku,
                                    "tech": tech,
                                    "wb": wb
                                })

                        connection.commit()
                        print(f"✅ Добавлен: {product_info['Артикул']}")

                    except Exception as e:
                        print(f"❌ Ошибка при обработке {product.get('vendorCode')}: {e}")
                        connection.rollback()

    except oracledb.Error as e:
        print(f"❌ Ошибка подключения к БД: {e}")
    










