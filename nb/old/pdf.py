import oracledb
from paramet import username, password, host, port, service_name
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
import os

# 📄 Генерация PDF
pdf_filename = "FILE-121.pdf"
c = canvas.Canvas(pdf_filename, pagesize=A4)
c.drawString(100, 800, "Пример PDF-документа для загрузки в Oracle")
c.save()
print(f"✅ PDF '{pdf_filename}' создан")

# 🔗 Подключение к Oracle
dsn = f"(DESCRIPTION=(ADDRESS=(PROTOCOL=TCP)(HOST={host})(PORT={port}))(CONNECT_DATA=(SERVICE_NAME={service_name})))"
connection = oracledb.connect(user=username, password=password, dsn=dsn)
cursor = connection.cursor()

# 📥 Чтение PDF
with open(pdf_filename, "rb") as file:
    pdf_data = file.read()
    
cursor.execute("delete fullbox.FB_FILE")
connection.commit()
# 💾 Вставка в таблицу 'documents'
sql = """
    INSERT INTO fullbox.FB_FILE ( FILE_NAME, FILE_DATA)
    VALUES (:1, :2)
"""
cursor.execute(sql, (pdf_filename, pdf_data))

# ✅ Подтверждаем
connection.commit()

# 🧹 Закрываем соединение
cursor.close()
connection.close()

print("🚀 Файл успешно загружен в Oracle DB!")
