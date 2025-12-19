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
        'FB - ' || f.id as NUMB,
        u.LASTNAME || ' ' || u.FIRSTNAME as USER_NAME,
        f.COUN_TOVAR,
        f.AGN_NAME,
        f.cr_date,
        f.ITOG_PRICE as SUUUUMM,
        'FB-'||f.ID NN,
        f.USE_NDS,
        nvl(DOGOVOR_NUMB, '___') do_num,
        nvl(f.AGN_USER, '___ФИО___') as fio
    from fullbox.FB_CALCULATOR f
    -- inner join fullbox.FB_AGNS a on a.id = f.AGN_ID
    inner join fullbox.fb_users u on u.auth = f.CR_USER
    where f.id = :param
)
select 
    doc.AGN_NAME, 
    TOVAR_NAME, 
    COUN_TOVAR, 
    SUUUUMM, 
    to_char(CR_DATE, 'dd.mm.yyyy') as CR_DATE, 
    NUMB, 
    USER_NAME, 
    round(SUUUUMM / COUN_TOVAR, 2) as SUM_ONE,
    NN,
    USE_NDS,
    DO_NUM,
    fio
from doc
"""

# Запрос для таблицы
sql2 = """
with doc as (select l.P_NAME,
       p.P_COUN,
       PRICE_USLUG COROB_PRICE,
       trunc(PRICE_USLUG/p.P_COUN) as SUUUUMM,
       case
       when c.USE_NDS = 1 then PRICE_USLUG + PRICE_USLUG/20
       when c.USE_NDS = 0 or c.USE_NDS is null then PRICE_USLUG
       else null 
       end price_nds,
       PNUM
from fullbox.FB_CALCULATOR_PID p
inner join fullbox.FB_CALCULATOR c
on c.id = p.pid
inner join fullbox.FB_CALCULATOR_POKAZAT_LIST l
on l.id = p.POKAZ_ID
inner join fullbox.FB_CALCULATOR_POKAZAT_LIST_TYPE t
on t.id = l.TYPE_ID
where pid = :param
--Коробы
union all
select 'Формирование транспортировочного короба' POKAZ_ID,
       COUNT_COROB,
       COROB_PRICE,
       COROB_PRICE / COUNT_COROB,
       case
       when f.USE_NDS = 1 then COROB_PRICE + COROB_PRICE/20
       when f.USE_NDS = 0 or f.USE_NDS is null then COROB_PRICE
       else null 
       end price_nds,
       8.4 pnum
from fullbox.FB_CALCULATOR f
where id = :param
--Паллеты
union all
select 'Сборка паллета с учетом евро палета' POKAZ_ID,
       COUNT_PALLET,
       PALLET_PRICE,
       PALLET_PRICE / COUNT_PALLET,
       case
       when ff.USE_NDS = 1 then PALLET_PRICE + PALLET_PRICE/20
       when ff.USE_NDS = 0 or ff.USE_NDS is null then PALLET_PRICE
       else null 
       end price_nds,
       8.5 pnum
from fullbox.FB_CALCULATOR ff
where id = :param)
select * from doc order by pnum
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


# Генерация PDF
# Генерация PDF с учётом требований
# def generate_pdf(filename, image_path, table_data, header_info):
#     c = canvas.Canvas(filename, pagesize=A4)
#     width, height = A4
#     margin = 50
#     current_y = height

#     agn_name = header_info['AGN_NAME']
#     numb = header_info['NUMB']
#     date = header_info['CR_DATE']
#     user_name = header_info['USER_NAME']
#     total_sum = header_info['SUUUUMM']
#     title_text = f"Коммерческое предложение № {numb} от {date}"

#     # Логотип в левом верхнем углу
#     if os.path.exists(image_path):
#         img_width, img_height = 80, 80 #!!!!!!!!!!!!!!!!!!!!!!!!!!
#         c.drawImage(image_path, margin, height - img_height - margin, width=img_width, height=img_height, preserveAspectRatio=True, mask='auto')
#     else:
#         print("⚠ Логотип не найден!")

#     # Приложение в правом верхнем углу
#     application_text = f"Приложение № 3 к Договору на оказание фулфилмент услуг № {numb} от {date}"
#     c.setFont("DejaVuSans", 10)
#     c.setFillColor(colors.black)
#     c.drawString(width - margin - c.stringWidth(application_text, "DejaVuSans", 10), height - margin - 20, application_text)

#     # Заголовок жирным "Коммерческое предложение"
    
    
#     c.setFont("DejaVuSans-Bold", 14)
#     c.setFillColor(colors.black)
#     proposal_text = f"Коммерческое предложение от ООО «Фуллбокс» (Исполнителя) на {date} г."
#     c.drawString(margin, current_y, proposal_text)
#     current_y -= 30  # Отступ после заголовка
#     current_y = height - 250  # отступ после логотипа, "приложения" и заголовка

#     # Таблица
#     total_table_width = width - 2 * margin
#     col_widths = [total_table_width * w for w in [0.3, 0.12, 0.2, 0.2, 0.18]]
#     table = Table(table_data, colWidths=col_widths)
#     table.setStyle(TableStyle([
#         ('FONTNAME', (0, 0), (-1, -1), 'DejaVuSans'),
#         ('FONTSIZE', (0, 0), (-1, -1), 10),
#         ('BACKGROUND', (0, 0), (-1, 0), colors.lightgrey),
#         ('TEXTCOLOR', (0, 0), (-1, 0), colors.black),
#         ('ALIGN', (0, 0), (-1, 0), 'CENTER'),
#         ('ALIGN', (1, 1), (-1, -1), 'CENTER'),
#         ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
#         ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
#         ('TOPPADDING', (0, 0), (-1, -1), 6),
#         ('VALIGN', (0, 0), (-1, -1), 'TOP'),
#     ]))
#     # table.wrapOn(c, width, height)
#     # table_height = table._height
#     # table.drawOn(c, margin, current_y - table_height)
#     # current_y -= table_height + 20
#     table_width, table_height = table.wrap(0, 0)
#     table.drawOn(c, margin, current_y - table_height)
#     current_y = current_y - table_height - 40  # Делаем хороший отступ после таблицы

#     # Проверка на достаточное место под следующий блок
#     if current_y < 150:
#         c.showPage()
#         current_y = height - margin



#     # Текст о федеральном законе
#     law_text = """** В связи с вступлением в силу Федерального закона от 12.07.2024 № 176-ФЗ "О внесении 
#                     изменений в части первую и вторую Налогового кодекса Российской Федерации, отдельные 
#                     законодательные акты Российской Федерации и признании утратившими силу отдельных 
#                     положений законодательных актов Российской Федерации" и, соответственно, в связи с 
#                     тем, что с 01 января 2025 года Исполнитель признан плательщиком налога на добавленную 
#                     стоимость, таким образом, стоимость услуг Исполнителя будет включен НДС действующий в 
#                     соответствии со ст. 164 НК РФ на момент подписания акта оказанных услуг, согласно п. 5.1 
#                     настоящего Договора.  
#     """
#     c.setFont("DejaVuSans", 10)
#     c.setFillColor(colors.black)
#     text_object = c.beginText(margin, current_y)
#     text_object.setFont("DejaVuSans", 10)
#     text_object.textLines(law_text)
#     c.drawText(text_object)
#     current_y -= 60  # Отступ после текста закона

#     # Подписи сторон
#     signature_text = """
#     ПОДПИСИ СТОРОН:
#     Исполнитель
#     ____________/ Опря С.Н./
#     (подпись)
#     (М.П.)
#     (Ф.И.О.)
#     Заказчик
#     ____________/Заруцкий С.П./
#     (подпись)
#     (М.П.)
#     (Ф.И.О.)
#     """
#     c.setFont("DejaVuSans", 10)
#     c.setFillColor(colors.black)
#     text_object = c.beginText(margin, 100)
#     text_object.setFont("DejaVuSans", 10)
#     # text_object.textLines(signature_text)
    
#     c.drawText(text_object)

#     # --- Подписи сторон ---
#     signature_y = 120  # Высота от нижнего края страницы

#     # Левая сторона (Исполнитель)
#     c.setFont("DejaVuSans", 10)
#     c.drawString(margin, signature_y, "Исполнитель")
#     c.drawString(margin, signature_y - 15, "______________/ Опря С.Н./")
#     c.drawString(margin, signature_y - 30, "(подпись)")
#     c.drawString(margin, signature_y - 45, "(М.П.)")
#     c.drawString(margin, signature_y - 60, "(Ф.И.О.)")

#     # Правая сторона (Заказчик)
#     right_margin = width - 250  # Координата справа, подогнана под A4
#     c.drawString(right_margin, signature_y, "Заказчик")
#     c.drawString(right_margin, signature_y - 15, "______________/ Заруцкий С.П./")
#     c.drawString(right_margin, signature_y - 30, "(подпись)")
#     c.drawString(right_margin, signature_y - 45, "(М.П.)")
#     c.drawString(right_margin, signature_y - 60, "(Ф.И.О.)")


#     # Сохраняем PDF
#     c.save()






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


    num_dg = header_info['DO_NUM']
    cr_dt = header_info['CR_DATE']
    use_nds = header_info['USE_NDS']
    fio_l = header_info['FIO']
    fb_num = header_info['NN']

    app_text = f"""<para align=right>
    Приложение № 3<br/>
    к Договору на оказание фулфилмент<br/>
    услуг № {num_dg} от {cr_dt} г.
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
    title_text = f"<b>Коммерческое предложение от ООО «Фуллбокс» № {fb_num} на {date}</b>"
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
    on_ed = header_info['SUM_ONE']
    story.append(Paragraph("Благодарим за интерес к нашим услугам! Ниже представлено коммерческое предложение:", normal))
    story.append(Paragraph(f"Итоговая сумма предложения: {total:,.0f} ₽. Цена за обработку 1 товара: {on_ed:,.0f} ₽".replace(",", " "), normal))
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
    if (int(use_nds) == 1):
        law_text = """
        <para>
        <b>** В связи с вступлением в силу Федерального закона от 12.07.2024 № 176-ФЗ</b> "О внесении изменений в части первую и вторую Налогового кодекса Российской Федерации, отдельные законодательные акты Российской Федерации и признании утратившими силу отдельных положений законодательных актов Российской Федерации" и, соответственно, в связи с тем, что с 01 января 2025 года Исполнитель признан плательщиком налога на добавленную стоимость, таким образом, стоимость услуг Исполнителя будет включен НДС, действующий в соответствии со ст. 164 НК РФ на момент подписания акта оказанных услуг, согласно п. 5.1 настоящего Договора.
        </para>
        """
    if (int(use_nds) == 0):
        law_text = ''
    story.append(Paragraph(law_text, normal))
    story.append(Spacer(1, 40))

    # Подписи сторон
    sign_table = Table([
        [
            Paragraph("Исполнитель<br/>______________/ Опря С.Н./<br/>(подпись)<br/>(М.П.)<br/>(Ф.И.О.)", normal),
            Paragraph(f"""Заказчик<br/>______________/ {fio_l}/<br/>(подпись)<br/>(М.П.)<br/>(Ф.И.О.)""", normal)
        ]
    ], colWidths=[(width - margin*2) / 2] * 2)

    sign_table.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP')
    ]))
    story.append(sign_table)

    # Сборка PDF
    doc.build(story)





def scri_gg(param):
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
                        "SUM_ONE": round(row[7], 2),
                        "NN": row[8],
                        "USE_NDS": row[9],
                        "DO_NUM": row[10],
                        "FIO": row[11],
                    }
                else:
                    raise Exception("Не удалось получить данные по предложению")

                # Получаем таблицу
                cursor.execute(sql2, {"param": param})
                rows = cursor.fetchall()

                # Формируем таблицу
                if (int(header_info['USE_NDS']) == 1):
                    table_data = [[
                        Paragraph("<b>Услуга</b>", styleN),
                        "Кол-во",
                        "Сумма",
                        "Цена за 1 ед.",
                        """Цена c НДС"""
                    ]]
                if (int(header_info['USE_NDS']) == 0):
                    table_data = [[
                        Paragraph("<b>Услуга</b>", styleN),
                        "Кол-во",
                        "Сумма",
                        "Цена за 1 ед.",
                        """Цена без НДС"""
                    ]]

                #====================------============

                for r in rows:
                    name = Paragraph(r[0], styleN)
                    count = int(r[1])
                    total = float(r[2])
                    discounted = float(r[3])
                    itog_pr = float(r[4])
                    if (int(row[9]) == 1):
                        table_data.append([
                            name,
                            str(count),
                            f"{total:,.0f} ₽".replace(",", " "),
                            f"{discounted:,.0f} ₽".replace(",", " "),
                            f"{itog_pr:,.0f} ₽".replace(",", " "),
                        ])
                    if (int(row[9]) == 0):
                        table_data.append([
                            name,
                            str(count),
                            f"{total:,.0f} ₽".replace(",", " "),
                            f"{discounted:,.0f} ₽".replace(",", " "),
                            f"{itog_pr:,.0f} ₽".replace(",", " "),
                        ])

                # Генерация PDF
                generate_pdf(
                    f"""КП Фуллбокс-{param}.pdf""",
                    "new_photo.jpg",
                    table_data,
                    header_info,
                    param
                )

                print("✅ PDF успешно создан!")


                pdf_filename = f"""КП Фуллбокс-{param}.pdf"""
                
                # 📥 Чтение PDF
                with open(pdf_filename, "rb") as file:
                    pdf_data = file.read()
                    
                cursor.execute("delete fullbox.FB_FILE where cr_date < sysdate - 2")
                connection.commit()

                # 💾 Вставка в таблицу 'documents'
                sql = f"""
                    INSERT INTO fullbox.FB_FILE ( FILE_NAME, FILE_DATA, CLC_NUMB)
                    VALUES (:1, :2, {param})
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

# scri_gg(141)