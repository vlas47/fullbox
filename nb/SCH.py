import time
import subprocess
import sys
import os

# Путь к запускаемому скрипту синхранизации Юр лиц
script_path = r"E:\WEB_FB\BACK\nb\CR_AGN_SHED.py"

# Путь к запускаемому скрипту создания пдф для калькулятора
script_path_pdf = r"E:\WEB_FB\BACK\nb\final_pdf.py"

while True:
    try:
        # Получаем путь к текущему интерпретатору Python
        python_exe = sys.executable

        # Проверка на существование скрипта
        if os.path.exists(script_path):
            print(f"Запускаю {script_path} через {python_exe}")
            subprocess.run([python_exe, script_path])
        else:
            print(f"Скрипт не найден: {script_path}")



        # # Проверка на существование скрипта
        # if os.path.exists(script_path_pdf):
        #     print(f"Запускаю {script_path_pdf} через {python_exe}")
        #     subprocess.run([python_exe, script_path_pdf])
        # else:
        #     print(f"Скрипт не найден: {script_path_pdf}")

        # Ждём 5 минут
        time.sleep(300)

    except Exception as e:
        print(f"Ошибка: {e}")
        time.sleep(300)
