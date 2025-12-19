import oracledb
from paramet import username, password, host, port, service_name
import os
import requests
import json

def sync_ozon(paramI):

    param = paramI

    sql_tovar = f"""select ARTIKUL	
                    from fullbox.FB_TOVAR_LIST
                    where agn_id = :param"""

    sql_k = f"""select  MARCET_KEY, CLIENT_ID
                from fullbox.FB_AGNS_MARKET_ARTIKU
                where MARKET_ID = 3
                and AGN_ID = :param"""
    
    # Подключение к Oracle
    oracledb.init_oracle_client(lib_dir=None)
    dsn = f"{host}:{port}/{service_name}"

    try:
        with oracledb.connect(user=username, password=password, dsn=dsn) as connection:
            with connection.cursor() as cursor:
                print("✅ Подключение установлено. Получаем данные...")
                # Записывем данные
                cursor.execute(sql_k, {"param": param})
                data = cursor.fetchall()
                API_KEY = data[0][0]
                CLIENT_ID = data[0][1]
                ################################    
                cursor.execute(sql_tovar, {"param": param})
                data_art = cursor.fetchall()
                # Преобразуем в плоский список строк
                array = [item[0] for item in data_art]
                print(data_art)
                ################################
                
                url = 'https://api-seller.ozon.ru/v3/product/list'

                headers = {
                    'Client-Id': CLIENT_ID,
                    'Api-Key': API_KEY,
                    'Content-Type': 'application/json'
                }

                data = {
                    "filter": {
                        "visibility": "ALL"
                    },
                    "last_id": "",
                    "limit": 10
                }

                response = requests.post(url, headers=headers, data=json.dumps(data))

                def get_info_tovar(tovar_id):
                    headers = {
                        'Client-Id': CLIENT_ID,
                        'Api-Key': API_KEY,
                        'Content-Type': 'application/json'
                    }

                    url = 'https://api-seller.ozon.ru/v3/product/info/list'

                    # Подставь сюда product_id из /v3/product/list
                    data = {
                        "product_id": [tovar_id]
                    }

                    response = requests.post(url, headers=headers, data=json.dumps(data))

                    if response.status_code == 200:
                        product_info = response.json()

                        for item in product_info['items']:
                            if item['offer_id'] in array:
                                print(f'''Карточка уже создана {item['offer_id']}''')
                            else:
                                try:
                                    cursor.execute(f"""insert into FULLBOX.FB_TOVAR_LIST (NAME, NAME_PRINT, ARTIKUL, AGN_ID, TSIZE, MADE_IN, IMG, COLOR_NAME, BRAND, STOR_UNIT_ID,   TYPE_TOVAR, WEIGHT, VOLUME, LENGTH, WIDTH, HEIGHT, GENDER, SEASON, DOP_ITEM_NAME, COMPOSITION, MARKET_TYPE, TOVAR_CATEGORY) 
                                                                                VALUES ('{item['name']}',
                                                                                        '{item['name']}', 
                                                                                        '{item['offer_id']}', 
                                                                                        '{param}',
                                                                                        '', 
                                                                                        '', 
                                                                                        '{item['primary_image'][0] if item['primary_image'] else 'Нет'}',
                                                                                        '',
                                                                                        '',
                                                                                        1
                                                                                        ,''
                                                                                        ,0
                                                                                        ,0
                                                                                        ,0
                                                                                        ,0
                                                                                        ,0
                                                                                        ,''
                                                                                        ,''
                                                                                        ,''
                                                                                        ,''
                                                                                        ,3
                                                                                        ,''
                                                                                        )""")
                                    

                                    # try:
                                    #     for dt in data["sizes"]:
                                    #         print(dt["techSize"])
                                    #         print(dt["wbSize"])
                                    #         for dt1 in dt["skus"]:
                                    #             print(dt1)
                                    #             cursor.execute(f"""insert into fullbox.FB_TOVAR_LIST_SCHK (pid, SCHK, TECHSIZE, WBSIZE) 
                                    #                                 select max(id), '{dt1}', '{dt["techSize"]}', '{dt["wbSize"]}'
                                    #                                 from fullbox.FB_TOVAR_LIST""")
                                
                                    # except Exception as e:
                                    #     print(f"Ошибка при синхранизации: {e}")


                                    connection.commit()

                                except Exception as e:
                                    print(f"Ошибка при синхранизации: {e}")
                            
                    
                    else:
                        print(f"Ошибка {response.status_code}: {response.text}")

                

                if response.status_code == 200:
                    products = response.json()
                    for dt in products['result']['items']:
                        get_info_tovar(dt['product_id'])

                else:
                    print(f"Ошибка {response.status_code}: {response.text}")

                

    except oracledb.Error as e:
        print(f"""❌ Ошибка {e}""")

