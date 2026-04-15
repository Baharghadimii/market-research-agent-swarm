import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime


def send(report_content: str, notion_url: str) -> None:
    """Send a Gmail notification with report preview and Notion link."""

    sender = "bahareh.ghad@gmail.com"
    recipient = "bahareh.ghad@gmail.com"
    today = datetime.now().strftime("%B %d, %Y")
    summary = str(report_content)[:500] + "..."

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"📊 New Market Research Report — {today}"
    msg["From"] = sender
    msg["To"] = recipient

    html = f"""
    <html><body style="font-family: Arial, sans-serif; padding: 20px;">
    <h2>📊 Vancouver Baby Products Market Research</h2>
    <p>Your quarterly market research report is ready in Notion!</p>
    <hr>
    <h3>Preview</h3>
    <p style="color: #444;">{summary}</p>
    <hr><br>
    <a href="{notion_url}" style="background:#000;color:#fff;padding:12px 24px;
        text-decoration:none;border-radius:6px;font-weight:bold;">
        View Full Report in Notion →
    </a>
    <br><br>
    <p style="color:gray;font-size:12px;">
        Sent automatically by your Market Research Agent Swarm 🤖<br>
        {today}
    </p>
    </body></html>
    """

    msg.attach(MIMEText(html, "html"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(sender, os.getenv("GMAIL_APP_PASSWORD"))
            server.sendmail(sender, recipient, msg.as_string())
        print(f"\n✅ Email notification sent to {recipient}!")
    except Exception as e:
        print(f"\n❌ Email failed: {e}")
