import os
import pymysql
from datetime import date, timedelta, datetime
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import inch
from reportlab.platypus import (
    SimpleDocTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
    Image,
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
import smtplib
from email.message import EmailMessage

from config import (
    PP_DB_NAME,
    PP_REPORT_EMAIL_TO,
    PP_REPORT_PDF_PATH,
    SMTP_FROM,
    SMTP_HOST,
    SMTP_PASSWORD,
    SMTP_PORT,
    SMTP_USER,
    db_config,
)

DB_CONFIG = db_config(database=PP_DB_NAME)

LOGO_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "assets",
    "policypilot_logo.png",
)

BRAND_BLUE = colors.HexColor("#1e4a8c")
BRAND_LIGHT = colors.HexColor("#e8f0fb")
PAGE_BG = colors.HexColor("#f8fafc")
CARD_BG = colors.white
TEXT = colors.HexColor("#0f172a")
MUTED = colors.HexColor("#475569")
GRID = colors.HexColor("#cbd5e1")


def send_email_with_attachment(
    subject,
    body,
    to_emails,
    file_path,
    from_email,
    smtp_host,
    smtp_port,
    smtp_user,
    smtp_password,
):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_email
    msg["To"] = ", ".join(to_emails)
    msg.set_content(body)

    with open(file_path, "rb") as f:
        file_data = f.read()
        file_name = file_path.split("/")[-1]

    msg.add_attachment(
        file_data,
        maintype="application",
        subtype="pdf",
        filename=file_name,
    )

    with smtplib.SMTP(smtp_host, smtp_port) as server:
        server.starttls()
        server.login(smtp_user, smtp_password)
        server.send_message(msg)


def fetch_one(cursor, query, params=None):
    cursor.execute(query, params or ())
    return cursor.fetchone()


def fetch_all(cursor, query, params=None):
    cursor.execute(query, params or ())
    return cursor.fetchall()


def user_to_lead_ratio(total_leads, total_users):
    if not total_users:
        return 0
    return round(total_leads / total_users, 2)


def generate_report(output_path="policypilot_daily_report.pdf"):
    yesterday = date.today() - timedelta(days=1)

    conn = pymysql.connect(**DB_CONFIG)

    try:
        with conn.cursor() as cursor:
            total_users = fetch_one(cursor, """
                SELECT COALESCE(MAX(id), 0) AS total_users
                FROM pp_user
            """)["total_users"]

            total_leads = fetch_one(cursor, """
                SELECT COALESCE(MAX(id), 0) AS total_leads
                FROM pp_application_master
            """)["total_leads"]

            ulr = user_to_lead_ratio(total_leads, total_users)

            new_acquisitions = fetch_one(cursor, """
                SELECT COUNT(*) AS total
                FROM pp_user
                WHERE DATE(created_at) = %s
            """, (yesterday,))["total"]

            source_rows = fetch_all(cursor, """
                SELECT utm_source, COUNT(*) AS total
                FROM pp_user
                GROUP BY utm_source
                ORDER BY total DESC
            """)

    finally:
        conn.close()

    doc = SimpleDocTemplate(
        output_path,
        pagesize=A4,
        rightMargin=42,
        leftMargin=42,
        topMargin=36,
        bottomMargin=40,
    )

    styles = getSampleStyleSheet()

    title_style = ParagraphStyle(
        "TitleStyle",
        parent=styles["Title"],
        textColor=BRAND_BLUE,
        fontName="Helvetica-Bold",
        fontSize=22,
        leading=28,
        alignment=1,
        spaceAfter=6,
    )

    subtitle_style = ParagraphStyle(
        "SubtitleStyle",
        parent=styles["Normal"],
        textColor=MUTED,
        fontName="Helvetica",
        fontSize=10,
        leading=14,
        alignment=1,
        spaceAfter=18,
    )

    section_style = ParagraphStyle(
        "SectionStyle",
        parent=styles["Heading2"],
        textColor=BRAND_BLUE,
        fontName="Helvetica-Bold",
        fontSize=13,
        leading=17,
        spaceBefore=14,
        spaceAfter=8,
    )

    footer_style = ParagraphStyle(
        "FooterStyle",
        parent=styles["Normal"],
        textColor=MUTED,
        fontName="Helvetica",
        fontSize=9,
        alignment=1,
    )

    story = []

    if os.path.exists(LOGO_PATH):
        logo = Image(LOGO_PATH, width=2.4 * inch, height=0.55 * inch)
        logo.hAlign = "CENTER"
        story.append(logo)
        story.append(Spacer(1, 10))

    story.append(Paragraph("PolicyPilot Daily Report", title_style))
    story.append(Paragraph(
        f"Insurance Leads Platform | Report Date: {date.today()} | Data For: {yesterday}",
        subtitle_style,
    ))

    def make_table(data, col_widths=None):
        if col_widths is None:
            col_widths = [285, 170]
        table = Table(data, colWidths=col_widths, rowHeights=40)

        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), BRAND_BLUE),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, 0), 11),

            ("BACKGROUND", (0, 1), (-1, -1), CARD_BG),
            ("TEXTCOLOR", (0, 1), (-1, -1), TEXT),
            ("FONTNAME", (0, 1), (0, -1), "Helvetica-Bold"),
            ("FONTNAME", (1, 1), (-1, -1), "Helvetica"),
            ("FONTSIZE", (0, 1), (-1, -1), 10.5),

            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, BRAND_LIGHT]),

            ("ALIGN", (0, 0), (-1, -1), "LEFT"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),

            ("LEFTPADDING", (0, 0), (-1, -1), 14),
            ("RIGHTPADDING", (0, 0), (-1, -1), 14),
            ("TOPPADDING", (0, 0), (-1, -1), 8),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 8),

            ("GRID", (0, 0), (-1, -1), 0.5, GRID),
            ("BOX", (0, 0), (-1, -1), 1, BRAND_BLUE),
        ]))

        return table

    story.append(Paragraph("Overall Metrics", section_style))
    story.append(make_table([
        ["Metric", "Value"],
        ["Total Users", total_users],
        ["Total Leads", total_leads],
        ["ULR (User to Lead Ratio)", f"{ulr} leads per user"],
    ]))

    story.append(Spacer(1, 12))

    story.append(Paragraph("User Acquisition", section_style))
    story.append(make_table([
        ["Metric", "Value"],
        ["New Acquisitions Yesterday", new_acquisitions],
    ]))

    story.append(Spacer(1, 12))

    source_table_data = [["UTM Source", "Users"]]
    for row in source_rows:
        source = row["utm_source"] if row["utm_source"] else "(not set)"
        source_table_data.append([source, row["total"]])

    story.append(Paragraph("Users by Source", section_style))
    story.append(make_table(source_table_data, col_widths=[285, 170]))

    story.append(Spacer(1, 24))
    story.append(Paragraph(
        "PolicyPilot | Daily Business Intelligence",
        footer_style,
    ))

    def add_background(canvas, doc):
        canvas.saveState()
        canvas.setFillColor(PAGE_BG)
        canvas.rect(0, 0, A4[0], A4[1], fill=1, stroke=0)

        canvas.setStrokeColor(colors.HexColor("#e2e8f0"))
        canvas.setLineWidth(0.25)
        for x in range(0, int(A4[0]), 50):
            canvas.line(x, 0, x, A4[1])
        for y in range(0, int(A4[1]), 50):
            canvas.line(0, y, A4[0], y)

        canvas.restoreState()

    doc.build(
        story,
        onFirstPage=add_background,
        onLaterPages=add_background,
    )


if __name__ == "__main__":
    pdf_file = PP_REPORT_PDF_PATH

    generate_report(pdf_file)

    send_email_with_attachment(
        subject=f"PolicyPilot Daily Report - {datetime.now().date()}",
        body="Attached is the PolicyPilot daily business report.",
        to_emails=PP_REPORT_EMAIL_TO,
        file_path=pdf_file,
        from_email=SMTP_FROM,
        smtp_host=SMTP_HOST,
        smtp_port=SMTP_PORT,
        smtp_user=SMTP_USER,
        smtp_password=SMTP_PASSWORD,
    )
