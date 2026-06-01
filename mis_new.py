import json
import pymysql
from datetime import date, timedelta
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
import smtplib
from email.message import EmailMessage
from datetime import datetime

DB_CONFIG = {
    "host": "172.31.41.11",
    "user": "profuse",
    "password": "tripleseven7",
    "database": "mf",
    "cursorclass": pymysql.cursors.DictCursor
}


BRAND_ORANGE = colors.HexColor("#f59e0b")
DARK_BG = colors.HexColor("#070b18")
CARD_BG = colors.HexColor("#111827")
TEXT = colors.HexColor("#f8fafc")
MUTED = colors.HexColor("#cbd5e1")
GRID = colors.HexColor("#334155")

def send_email_with_attachment(
    subject,
    body,
    to_emails,
    file_path,
    from_email,
    smtp_host,
    smtp_port,
    smtp_user,
    smtp_password
):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_email
    msg["To"] = ", ".join(to_emails)

    msg.set_content(body)

    # Attach PDF
    with open(file_path, "rb") as f:
        file_data = f.read()
        file_name = file_path.split("/")[-1]

    msg.add_attachment(
        file_data,
        maintype="application",
        subtype="pdf",
        filename=file_name
    )

    # Send
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


def complete_user_ratio(complete_users, total_users):
    if not total_users:
        return 0
    return round((complete_users * 100) / total_users, 2)


def normalize_utm_source(utm_source):
    if utm_source is None:
        return "organic"
    if isinstance(utm_source, str):
        normalized = utm_source.strip()
        if not normalized or normalized.lower() == "null":
            return "organic"
        if normalized.lower() == "organic":
            return "organic"
        return normalized
    return str(utm_source)


def is_lead_sent(criteria_missed):
    if criteria_missed is None:
        return True
    if isinstance(criteria_missed, str):
        if not criteria_missed.strip():
            return True
        try:
            criteria_missed = json.loads(criteria_missed)
        except json.JSONDecodeError:
            return False
    if isinstance(criteria_missed, (list, tuple)):
        return len(criteria_missed) == 0
    return False


def generate_report(output_path="moneyfatafat_daily_report.pdf"):
    yesterday = date.today() - timedelta(days=1)

    conn = pymysql.connect(**DB_CONFIG)

    try:
        with conn.cursor() as cursor:
            total_users = fetch_one(cursor, """
                SELECT COALESCE(MAX(id), 0) AS total_users
                FROM mf_users
            """)["total_users"]

            status_rows = fetch_all(cursor, """
                SELECT status, COUNT(id) AS total
                FROM mf_users
                WHERE status IN (0, 1)
                GROUP BY status
            """)

            status_map = {row["status"]: row["total"] for row in status_rows}

            partial_users = status_map.get(0, 0)
            complete_users = status_map.get(1, 0)
            overall_cr = complete_user_ratio(complete_users, total_users)

            acquisition_rows = fetch_all(cursor, """
                SELECT status, COUNT(id) AS total
                FROM mf_users
                WHERE DATE(created_date) = %s
                  AND status IN (0, 1)
                GROUP BY status
            """, (yesterday,))

            acq_map = {row["status"]: row["total"] for row in acquisition_rows}

            new_acquisitions = acq_map.get(0, 0) + acq_map.get(1, 0)
            complete_yesterday = acq_map.get(1, 0)
            acquisition_cr = complete_user_ratio(complete_yesterday, new_acquisitions)

            utm_acq_rows = fetch_all(cursor, """
                SELECT utm_source, COUNT(*) AS users, status
                FROM mf_users
                WHERE DATE(created_date) = %s
                  AND status IN (0, 1)
                GROUP BY utm_source, status
            """, (yesterday,))

            utm_acquisitions = {}
            for row in utm_acq_rows:
                source = normalize_utm_source(row["utm_source"])
                if source not in utm_acquisitions:
                    utm_acquisitions[source] = {"complete": 0, "partial": 0}
                if row["status"] == 1:
                    utm_acquisitions[source]["complete"] += row["users"]
                else:
                    utm_acquisitions[source]["partial"] += row["users"]

            first_user_row = fetch_one(cursor, """
                SELECT id
                FROM mf_users
                WHERE DATE(created_date) = %s
                ORDER BY id ASC
                LIMIT 1
            """, (yesterday,))

            first_user_id = first_user_row["id"] if first_user_row else None

            yesterday_apps = fetch_one(cursor, """
                SELECT COUNT(id) AS total
                FROM application_master
                WHERE DATE(created_date) = %s
            """, (yesterday,))["total"]

            if first_user_id:
                fta = fetch_one(cursor, """
                    SELECT COUNT(id) AS total
                    FROM application_master
                    WHERE DATE(created_date) = %s
                      AND userid >= %s
                """, (yesterday, first_user_id))["total"]

                ra = fetch_one(cursor, """
                    SELECT COUNT(id) AS total
                    FROM application_master
                    WHERE DATE(created_date) = %s
                      AND userid < %s
                """, (yesterday, first_user_id))["total"]
            else:
                fta = 0
                ra = yesterday_apps

            lender_rows = fetch_all(cursor, """
                SELECT lender.lender_name, logs.criteria_missed
                FROM application_bre_logs AS logs
                JOIN mf_lenders AS lender ON logs.lender_id = lender.id
                WHERE application_id IN (
                    SELECT id FROM application_master WHERE DATE(created_date) = %s
                )
            """, (yesterday,))

            lender_leads = {}
            for row in lender_rows:
                name = row["lender_name"] or "Unknown"
                if is_lead_sent(row["criteria_missed"]):
                    lender_leads[name] = lender_leads.get(name, 0) + 1

    finally:
        conn.close()

    doc = SimpleDocTemplate(
        output_path,
        pagesize=A4,
        rightMargin=42,
        leftMargin=42,
        topMargin=40,
        bottomMargin=40
    )

    styles = getSampleStyleSheet()

    title_style = ParagraphStyle(
        "TitleStyle",
        parent=styles["Title"],
        textColor=TEXT,
        fontName="Helvetica-Bold",
        fontSize=24,
        leading=30,
        alignment=1,
        spaceAfter=8
    )

    subtitle_style = ParagraphStyle(
        "SubtitleStyle",
        parent=styles["Normal"],
        textColor=MUTED,
        fontName="Helvetica",
        fontSize=10,
        leading=14,
        alignment=1,
        spaceAfter=22
    )

    section_style = ParagraphStyle(
        "SectionStyle",
        parent=styles["Heading2"],
        textColor=BRAND_ORANGE,
        fontName="Helvetica-Bold",
        fontSize=14,
        leading=18,
        spaceBefore=18,
        spaceAfter=10
    )

    footer_style = ParagraphStyle(
        "FooterStyle",
        parent=styles["Normal"],
        textColor=MUTED,
        fontName="Helvetica",
        fontSize=9,
        alignment=1
    )

    story = []

    story.append(Paragraph(
        '<font color="#f59e0b">Moneyfatafat</font> Daily Report',
        title_style
    ))

    story.append(Paragraph(
        f"Instant Loans fata-fat | Report Date: {date.today()} | Data For: {yesterday}",
        subtitle_style
    ))

    def make_table(data, col_widths=None):
        if col_widths is None:
            col_widths = [285, 170]
        table = Table(data, colWidths=col_widths, rowHeights=42)

        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), BRAND_ORANGE),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.black),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, 0), 11),

            ("BACKGROUND", (0, 1), (-1, -1), CARD_BG),
            ("TEXTCOLOR", (0, 1), (-1, -1), TEXT),
            ("FONTNAME", (0, 1), (0, -1), "Helvetica-Bold"),
            ("FONTNAME", (1, 1), (1, -1), "Helvetica-Bold"),
            ("FONTSIZE", (0, 1), (-1, -1), 10.5),

            ("ALIGN", (0, 0), (-1, -1), "LEFT"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),

            ("LEFTPADDING", (0, 0), (-1, -1), 14),
            ("RIGHTPADDING", (0, 0), (-1, -1), 14),
            ("TOPPADDING", (0, 0), (-1, -1), 8),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 8),

            ("GRID", (0, 0), (-1, -1), 0.5, GRID),
            ("BOX", (0, 0), (-1, -1), 1.2, BRAND_ORANGE),
        ]))

        return table

    story.append(Paragraph("Overall Users", section_style))
    story.append(make_table([
        ["Metric", "Value"],
        ["Total Users", total_users],
        ["Total Partial Users", partial_users],
        ["Total Complete Users", complete_users],
        ["CR (Complete User Ratio)", f"{overall_cr}%"],
    ]))

    story.append(Spacer(1, 14))

    story.append(Paragraph("User Acquisition", section_style))
    story.append(make_table([
        ["Metric", "Value"],
        ["New Acquisitions Yesterday", new_acquisitions],
        ["Complete Users Yesterday", complete_yesterday],
        ["CR (Complete User Ratio)", f"{acquisition_cr}%"],
    ]))

    story.append(Spacer(1, 14))

    utm_table_data = [["UTM Source", "Complete Users", "Partial Users"]]
    for source, counts in sorted(
        utm_acquisitions.items(),
        key=lambda item: item[1]["complete"] + item[1]["partial"],
        reverse=True,
    ):
        utm_table_data.append([
            source,
            counts["complete"],
            counts["partial"],
        ])

    story.append(Paragraph("UTM-Source wise Acquisitions", section_style))
    story.append(make_table(utm_table_data, col_widths=[200, 127, 128]))

    story.append(Spacer(1, 14))

    story.append(Paragraph("Applications", section_style))
    story.append(make_table([
        ["Metric", "Value"],
        ["Yesterday Applications", yesterday_apps],
        ["FTA - First Time Applicant", fta],
        ["RA - Repeat Applicant", ra],
    ]))

    story.append(Spacer(1, 14))

    lender_table_data = [["Lender Name", "Leads", "Percentage Dist"]]
    for lender_name, leads in sorted(
        lender_leads.items(), key=lambda item: item[1], reverse=True
    ):
        pct = complete_user_ratio(leads, yesterday_apps)
        lender_table_data.append([lender_name, leads, f"{pct}%"])

    story.append(Paragraph("Lenderwise Distribution", section_style))
    story.append(make_table(lender_table_data, col_widths=[220, 115, 120]))

    story.append(Spacer(1, 28))

    story.append(Paragraph(
        "Secure | RBI Compliant | DPDP Act 2023",
        footer_style
    ))

    def add_background(canvas, doc):
        canvas.saveState()

        canvas.setFillColor(DARK_BG)
        canvas.rect(0, 0, A4[0], A4[1], fill=1, stroke=0)

        canvas.setStrokeColor(colors.HexColor("#172033"))
        canvas.setLineWidth(0.25)

        for x in range(0, int(A4[0]), 45):
            canvas.line(x, 0, x, A4[1])

        for y in range(0, int(A4[1]), 45):
            canvas.line(0, y, A4[0], y)

        canvas.restoreState()

    doc.build(
        story,
        onFirstPage=add_background,
        onLaterPages=add_background
    )


if __name__ == "__main__":
    generate_report()

    pdf_file = "/var/moneyfatafat_daily_report.pdf"

    generate_report(pdf_file)

    send_email_with_attachment(
        subject=f"Moneyfatafat Daily Report - {datetime.now().date()}",
        body="Attached is the daily business report.",
        to_emails=["anup.vaze@appkhichadi.com", "hiteshmittal@profuseservices.com","rishi.saraf@profuseservices.com", "sravya@profuseservices.com"],
        #to_emails=["anup.vaze@appkhichadi.com"],
        file_path=pdf_file,
        from_email="anup.vaze@appkhichadi.com",
        smtp_host="smtp.gmail.com",
        smtp_port=587,
        smtp_user="anup.vaze@appkhichadi.com",
        smtp_password="qjqn sbpr yvso seiq"
    )
