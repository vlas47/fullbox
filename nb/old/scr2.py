import oracledb
from paramet import username, password, host, port, service_name
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Table, TableStyle, Paragraph
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet
import os

# Подключение к Oracle
oracledb.init_oracle_client(lib_dir=None)
dsn = f"{host}:{port}/{service_name}"
param = 121 #41  # ID калькуляции

# SQL-запрос с расширенными полями
sql1 = """
with doc as (
    select 
        f.TOVAR_NAME, 
        a.PREF || '-' || f.id as NUMB,
        u.LASTNAME || ' ' || u.FIRSTNAME as USER_NAME,
        f.COUN_TOVAR,
        a.AGN_NAME,
        f.cr_date,
        sum(l.P_PRICE * p.p_coun - (nvl(DISCOU, 0)/100 * l.P_PRICE * p.p_coun)) as SUUUUMM
    from fullbox.FB_CALCULATOR f
    inner join fullbox.FB_CALCULATOR_PID p on p.pid = f.id
    inner join fullbox.FB_CALCULATOR_POKAZAT_LIST l on l.id = p.POKAZ_ID
    inner join fullbox.FB_AGNS a on a.id = f.AGN_ID
    inner join fullbox.fb_users u on u.auth = f.CR_USER
    where f.id = :param
    group by f.TOVAR_NAME, f.COUN_TOVAR, a.AGN_NAME, f.cr_date,
             a.PREF, f.id, u.LASTNAME, u.FIRSTNAME
)
select 
    AGN_NAME, 
    TOVAR_NAME, 
    COUN_TOVAR, 
    SUUUUMM, 
    to_char(CR_DATE, 'dd.mm.yyyy') as CR_DATE, 
    NUMB, 
    USER_NAME, 
    SUUUUMM / COUN_TOVAR as SUM_ONE
from doc
"""

# Запрос для таблицы
sql2 = """
select l.P_NAME, 
       p.P_COUN,
       l.P_PRICE * p.p_coun, 
       l.P_PRICE * p.p_coun - (nvl(DISCOU, 0)/100 * l.P_PRICE * p.p_coun) as SUUUUMM
from fullbox.FB_CALCULATOR f
inner join fullbox.FB_CALCULATOR_PID p on p.pid = f.id
inner join fullbox.FB_CALCULATOR_POKAZAT_LIST l on l.id = p.POKAZ_ID
where f.id = :param
"""

# Стили PDF
pdfmetrics.registerFont(TTFont('DejaVuSans', 'DejaVuSans.ttf'))
styles = getSampleStyleSheet()
styleN = styles["Normal"]
styleN.fontName = "DejaVuSans"
styleN.fontSize = 10
styleN.leading = 12

# Генерация PDF
def generate_pdf(filename, image_path, table_data, header_info):
    c = canvas.Canvas(filename, pagesize=A4)
    width, height = A4
    margin = 50
    current_y = height

    agn_name = header_info['AGN_NAME']
    numb = header_info['NUMB']
    date = header_info['CR_DATE']
    user_name = header_info['USER_NAME']
    total_sum = header_info['SUUUUMM']
    title_text = f"Коммерческое предложение № {numb} от {date}"

    # Шапка с изображением
    if os.path.exists(image_path):
        img_height = 180
        c.drawImage(image_path, 0, height - img_height, width=width, height=img_height, preserveAspectRatio=True, mask='auto')
        current_y = height - img_height - 20
    else:
        print("⚠ Изображение не найдено!")

    # Заголовок
    c.setFont("DejaVuSans", 16)
    c.setFillColor(colors.darkblue)
    c.drawString(margin, current_y, title_text)

    current_y -= 25
    c.setFont("DejaVuSans", 10)
    c.setFillColor(colors.black)
    c.drawString(margin, current_y, f"Менеджер: {user_name}")
    c.drawString(margin + 250, current_y, f"Для клиента: {agn_name}")

    current_y -= 20
    c.drawString(margin, current_y, "Благодарим за интерес к нашим услугам! Ниже представлено коммерческое предложение:")

    current_y -= 20
    c.drawString(margin, current_y, f"Итоговая сумма предложения: {total_sum:,.0f} ₽".replace(",", " "))

    current_y -= 40  # Отступ перед таблицей

    # Таблица
    total_table_width = width - 2 * margin
    col_widths = [total_table_width * w for w in [0.3, 0.12, 0.2, 0.2, 0.18]]

    table = Table(table_data, colWidths=col_widths)
    table.setStyle(TableStyle([
        ('FONTNAME', (0, 0), (-1, -1), 'DejaVuSans'),
        ('FONTSIZE', (0, 0), (-1, -1), 10),
        ('BACKGROUND', (0, 0), (-1, 0), colors.lightgrey),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.black),
        ('ALIGN', (0, 0), (-1, 0), 'CENTER'),
        ('ALIGN', (1, 1), (-1, -1), 'CENTER'),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ('TOPPADDING', (0, 0), (-1, -1), 6),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
    ]))

    table.wrapOn(c, width, height)
    table_height = table._height
    table.drawOn(c, margin, current_y - table_height)

    c.save()

# Основной блок
try:
    with oracledb.connect(user=username, password=password, dsn=dsn) as connection:
        with connection.cursor() as cursor:
            print("✅ Подключение установлено. Получаем данные...")

            # Получаем информацию из sql1
            cursor.execute(sql1, {"param": param})
            row = cursor.fetchone()
            if row:
                header_info = {
                    "AGN_NAME": row[0],
                    "TOVAR_NAME": row[1],
                    "COUN_TOVAR": row[2],
                    "SUUUUMM": float(row[3]),
                    "CR_DATE": row[4],
                    "NUMB": row[5],
                    "USER_NAME": row[6],
                    "SUM_ONE": round(row[7], 2)
                }
            else:
                raise Exception("Не удалось получить данные по предложению")

            # Получаем таблицу
            cursor.execute(sql2, {"param": param})
            rows = cursor.fetchall()

            # Формируем таблицу
            table_data = [[
                Paragraph("<b>Услуга</b>", styleN),
                "Кол-во",
                "Сумма",
                "С учётом скидки",
                "Цена за 1 ед."
            ]]

            for r in rows:
                name = Paragraph(r[0], styleN)
                count = int(r[1])
                total = float(r[2])
                discounted = float(r[3])
                table_data.append([
                    name,
                    str(count),
                    f"{total:,.0f} ₽".replace(",", " "),
                    f"{discounted:,.0f} ₽".replace(",", " "),
                    f"{header_info['SUM_ONE']:,.2f} ₽".replace(",", " ")
                ])

            # Генерация PDF
            generate_pdf(
                f"""FILE-{param}.pdf""",
                "photo.jpg",
                table_data,
                header_info
            )

            print("✅ PDF успешно создан!")

except oracledb.Error as e:
    print("❌ Ошибка подключения или выполнения запроса:")
    print(e)
