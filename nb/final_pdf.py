import oracledb
from paramet import username, password, host, port, service_name
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
# from reportlab.platypus import Table, TableStyle, Paragraph
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
import os
import sys
from reportlab.platypus import SimpleDocTemplate, Paragraph, Table, TableStyle, Spacer, Image
from reportlab.lib.units import mm



# Подключение к Oracle
oracledb.init_oracle_client(lib_dir=None)
dsn = f"{host}:{port}/{service_name}"
# param = 141 #  # ID калькуляции

# param = int(sys.argv[1])

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
select 'Цены за упаковочные короба' as P_NAME, COUNT_COROB P_COUN, COROB_PRICE, trunc(COROB_PRICE/COUNT_COROB) as SUUUUMM
from fullbox.FB_CALCULATOR
where id = :param
union all
select l.P_NAME, 
       p.P_COUN,
       l.P_PRICE * p.p_coun, 
    --    l.P_PRICE * p.p_coun - (nvl(DISCOU, 0)/100 * l.P_PRICE * p.p_coun) as SUUUUMM
    trunc(l.P_PRICE * p.p_coun/p.P_COUN) as SUUUUMM
from fullbox.FB_CALCULATOR f
inner join fullbox.FB_CALCULATOR_PID p on p.pid = f.id
inner join fullbox.FB_CALCULATOR_POKAZAT_LIST l on l.id = p.POKAZ_ID
where f.id = :param
"""

# Стили PDF
# Стили PDF
pdfmetrics.registerFont(TTFont('DejaVuSans', 'DejaVuSans.ttf'))
pdfmetrics.registerFont(TTFont('DejaVuSans-Bold', 'DejaVuSans-Bold.ttf'))  # <-- добавлено
styles = getSampleStyleSheet()
styleN = styles["Normal"]
styleN.fontName = "DejaVuSans"
styleN.fontSize = 10
styleN.leading = 12



def generate_pdf(filename, image_path, table_data, header_info, param):
    width, height = A4
    margin = 50

    # Регистрируем шрифты
    pdfmetrics.registerFont(TTFont('DejaVuSans', 'DejaVuSans.ttf'))
    pdfmetrics.registerFont(TTFont('DejaVuSans-Bold', 'DejaVuSans-Bold.ttf'))

    styles = getSampleStyleSheet()
    normal = ParagraphStyle(
        'Normal',
        parent=styles['Normal'],
        fontName='DejaVuSans',
        fontSize=10,
        leading=12
    )
    bold = ParagraphStyle(
        'Bold',
        parent=styles['Normal'],
        fontName='DejaVuSans-Bold',
        fontSize=12,
        leading=14
    )

    # Документ
    doc = SimpleDocTemplate(filename, pagesize=A4,
                            leftMargin=margin, rightMargin=margin,
                            topMargin=40, bottomMargin=40)

    story = []

    # Верхняя часть — логотип и "Приложение № 3..."
    elements = []

    if os.path.exists(image_path):
        img = Image(image_path, width=250, height=60)
    else:
        print("⚠ Изображение не найдено!")
        img = Spacer(60, 60)

    app_text = """<para align=right>
    Приложение № 3<br/>
    к Договору на оказание фулфилмент<br/>
    услуг № 16/04/25-Н от 16.04.2025 г.
    </para>"""
    app_paragraph = Paragraph(app_text, normal)

    from reportlab.platypus import Table as PlatypusTable
    layout_table = PlatypusTable([[img, app_paragraph]], colWidths=[80, width - margin*2 - 80])
    layout_table.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('ALIGN', (1, 0), (1, 0), 'RIGHT')
    ]))
    story.append(layout_table)
    story.append(Spacer(1, 20))

    # Заголовок
    date = header_info['CR_DATE']
    title_text = f"<b>Коммерческое предложение от ООО «Фуллбокс» (Исполнителя) на {date}</b>"
    story.append(Paragraph(title_text, bold))
    story.append(Spacer(1, 10))

    # Менеджер и клиент
    user = header_info["USER_NAME"]
    agn = header_info["AGN_NAME"]
    story.append(Paragraph(f"Менеджер: {user}", normal))
    story.append(Paragraph(f"Для клиента: {agn}", normal))
    story.append(Spacer(1, 10))

    # Сумма
    total = header_info['SUUUUMM']
    story.append(Paragraph("Благодарим за интерес к нашим услугам! Ниже представлено коммерческое предложение:", normal))
    story.append(Paragraph(f"Итоговая сумма предложения: {total:,.0f} ₽".replace(",", " "), normal))
    story.append(Spacer(1, 20))

    # Таблица
    total_width = width - margin * 2  # Ширина с учётом отступов
    col_widths = [total_width * w for w in [0.3, 0.12, 0.2, 0.2, 0.18]]

    # col_widths = [width * 0.3, width * 0.12, width * 0.2, width * 0.2, width * 0.18]
    table = Table(table_data, colWidths=col_widths)
    table.setStyle(TableStyle([
        ('FONTNAME', (0, 0), (-1, -1), 'DejaVuSans'),
        ('FONTSIZE', (0, 0), (-1, -1), 9),
        ('BACKGROUND', (0, 0), (-1, 0), colors.lightgrey),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.black),
        ('ALIGN', (1, 1), (-1, -1), 'CENTER'),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
        ('TOPPADDING', (0, 0), (-1, -1), 5),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
    ]))
    story.append(table)
    story.append(Spacer(1, 20))

    # Текст закона
    law_text = """
    <para>
    <b>** В связи с вступлением в силу Федерального закона от 12.07.2024 № 176-ФЗ</b> "О внесении изменений в части первую и вторую Налогового кодекса Российской Федерации, отдельные законодательные акты Российской Федерации и признании утратившими силу отдельных положений законодательных актов Российской Федерации" и, соответственно, в связи с тем, что с 01 января 2025 года Исполнитель признан плательщиком налога на добавленную стоимость, таким образом, стоимость услуг Исполнителя будет включен НДС, действующий в соответствии со ст. 164 НК РФ на момент подписания акта оказанных услуг, согласно п. 5.1 настоящего Договора.
    </para>
    """
    story.append(Paragraph(law_text, normal))
    story.append(Spacer(1, 40))

    # Подписи сторон
    sign_table = Table([
        [
            Paragraph("Исполнитель<br/>______________/ Опря С.Н./<br/>(подпись)<br/>(М.П.)<br/>(Ф.И.О.)", normal),
            Paragraph("Заказчик<br/>______________/ Заруцкий С.П./<br/>(подпись)<br/>(М.П.)<br/>(Ф.И.О.)", normal)
        ]
    ], colWidths=[(width - margin*2) / 2] * 2)

    sign_table.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP')
    ]))
    story.append(sign_table)

    # Сборка PDF
    doc.build(story)





def scri_gg():
    # Основной блок
    try:
        with oracledb.connect(user=username, password=password, dsn=dsn) as connection:
            with connection.cursor() as cursor:
                print("✅ Подключение установлено. Получаем данные...")

                # cursor.execute("delete fullbox.FB_FILE where cr_date > sysdate - 1")
                # connection.commit()
                cursor.execute(f"""select id from fullbox.FB_CALCULATOR where cr_pdf = 1 and id not in (select CLC_NUMB from fullbox.FB_FILE)""")
                f_num = cursor.fetchall()

                for param in f_num:
                    print(param[0])
                    param0 = param[0]
                    # Получаем информацию из sql1
                    cursor.execute(sql1, {"param": param0})
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
                    cursor.execute(sql2, {"param": param0})
                    rows = cursor.fetchall()

                    # Формируем таблицу
                    table_data = [[
                        Paragraph("<b>Услуга</b>", styleN),
                        "Кол-во",
                        "Сумма",
                        "Цена за 1 ед.",
                        """Цена за 1 ед 
    c НДС 5%"""
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
                            f"{discounted:,.0f} ₽".replace(",", " "),
                        ])

                    # Генерация PDF
                    generate_pdf(
                        f"""КП Фуллбокс-{param0}.pdf""",
                        "new_photo.jpg",
                        table_data,
                        header_info,
                        param0
                    )

                    print("✅ PDF успешно создан!")


                    pdf_filename = f"""КП Фуллбокс-{param0}.pdf"""
                    
                    # 📥 Чтение PDF
                    with open(pdf_filename, "rb") as file:
                        pdf_data = file.read()
                        
                    cursor.execute(f"""update fullbox.FB_CALCULATOR
                                        set CR_PDF = 0
                                        where id = {param0}""")
                    connection.commit()
                    # 💾 Вставка в таблицу 'documents'
                    sql = f"""
                        INSERT INTO fullbox.FB_FILE ( FILE_NAME, FILE_DATA, CLC_NUMB)
                        VALUES (:1, :2, {param0})
                    """
                    cursor.execute(sql, (pdf_filename, pdf_data))

                    # ✅ Подтверждаем
                    connection.commit()

                    # 🧹 Закрываем соединение
                cursor.close()
                connection.close()

                print("🚀 Файл успешно загружен в Oracle DB!")

    except oracledb.Error as e:
        print("❌ Ошибка подключения или выполнения запроса:")
        print(e)

scri_gg()