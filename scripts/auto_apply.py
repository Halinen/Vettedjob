"""
Automated email submission — only for jobs with applying status + email method + PDFs ready.
Before running, confirm that both cl/cl.pdf and cv/cv.pdf have been compiled.

TODO: Feature not yet complete. Known issues:
  - status and application_method are stored in jobs_index.csv, not status.json,
    so this needs to read the corresponding fields from jobs_index.csv before deciding whether to submit.
"""

import json
import os
import smtplib
from datetime import date
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

JOBS_DIR = Path("jobs")


def apply_by_email(folder: Path, status: dict):
    contact = status.get("contact_email", "")
    if not contact:
        print(f"  [{folder.name}] No contact email, skipping automated submission")
        return

    cl_pdf = folder / "cl" / "cl.pdf"
    cv_pdf = folder / "cv" / "cv.pdf"
    if not cl_pdf.exists() or not cv_pdf.exists():
        print(f"  [{folder.name}] PDF not compiled, skipping")
        return

    cl_body = (folder / "job_info.md").read_text()[:500]

    msg = MIMEMultipart()
    msg["Subject"] = f"Application for {status['title']} – Bo Yuan"
    msg["From"] = os.environ["EMAIL_FROM"]
    msg["To"] = contact
    msg.attach(MIMEText(
        f"Dear Hiring Committee,\n\n"
        f"Please find attached my application for the position of {status['title']}.\n\n"
        f"Best regards,\nBo Yuan\nboyua@kth.se | +46 769612787",
        "plain", "utf-8"
    ))

    for pdf_path, filename in [(cl_pdf, "CoverLetter_BoYuan.pdf"),
                                (cv_pdf, "CV_BoYuan.pdf")]:
        with open(pdf_path, "rb") as f:
            att = MIMEApplication(f.read(), _subtype="pdf")
            att.add_header("Content-Disposition", "attachment", filename=filename)
            msg.attach(att)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(os.environ["EMAIL_FROM"], os.environ["EMAIL_APP_PASSWORD"])
        server.send_message(msg)

    # Update status
    status["status"] = "applied"
    status["timeline"].append({
        "date": date.today().isoformat(),
        "event": "applied",
        "note": f"Automated email submission to {contact}"
    })
    (folder / "status.json").write_text(
        json.dumps(status, ensure_ascii=False, indent=2))
    print(f"  ✓ Submitted: {status['title']} → {contact}")


def run():
    for folder in sorted(JOBS_DIR.iterdir()):
        sf = folder / "status.json"
        if not sf.exists():
            continue
        status = json.loads(sf.read_text())
        if (status.get("status") == "applying"
                and status.get("application_method") == "email"):
            apply_by_email(folder, status)


if __name__ == "__main__":
    run()
