# python -m uvicorn FApiT:app --reload

from fastapi import FastAPI
from fastapi.responses import FileResponse
import os
import subprocess
from old.new_scr import scri_gg
from sync_wb import sync_wb

from sync_ozon import sync_ozon

app = FastAPI()

@app.get("/get-pdf")
def get_pdf():
    pdf_path = "FILE-141.pdf"  # путь к файлу
    if os.path.exists(pdf_path):
        return FileResponse(path=pdf_path, media_type='application/pdf', filename="FILE-141.pdf")
    return {"error": "File not found"}




@app.get("/sync-wb/{value}")
def sync_wldb(value: int):
    sync_wb(value)


# @app.get("/sync-ozon/{value}")
# def sync_wldb(value: int):
#     sync_ozon(value)



@app.get("/run-script/{value}")
def run_script(value: int):
    print(value)
    print(value)
    scri_gg(value)
    # try:
    #     # Запускаем новый скрипт с передачей значения
    #     result = subprocess.run(
    #         ["python3", "new_scr.py", str(value)],  # Запускаем скрипт и передаем параметр
    #         capture_output=True,  # Перехватываем вывод
    #         text=True  # Получаем вывод как строку
    #     )

    #     if result.returncode == 0:
    #         # Возвращаем вывод скрипта, если всё прошло успешно
    #         return {"status": "success", "output": result.stdout}
    #     else:
    #         return {"status": "error", "message": result.stderr}
    
    # except Exception as e:
    #     return {"status": "error", "message": str(e)}

