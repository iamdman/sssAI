import email, smtplib, ssl

from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

#Setup a burner gmail account to send emails and follow links below
#to ensure the smtp connection will work
#https://www.google.com/settings/security/lesssecureapps
#https://accounts.google.com/DisplayUnlockCaptcha

sender_email = "XXXYOURGMAILADDRESSXXX@gmail.com"
smtp_host = "smtp.gmail.com"
smtp_port = 587
password = "XXXYOURGMAILPASSWORDXXX"

def sendmail(sender_email, receiver_email, subject, html, filename) -> bool:
   # Create a multipart message and set headers
   message = MIMEMultipart()
   message["From"] = sender_email
   message["To"] = receiver_email
   message["Subject"] = subject
   message["Bcc"] = receiver_email  # Recommended for mass emails

   # Turn these into plain/html MIMEText objects
   message.attach(MIMEText(html, "html"))

   if filename:
      # Open PDF file in binary mode
      with open(filename, "rb") as attachment:
          # Add file as application/octet-stream
          # Email client can usually download this automatically as attachment
          part = MIMEBase("application", "octet-stream")
          part.set_payload(attachment.read())

      # Encode file in ASCII characters to send by email    
      encoders.encode_base64(part)

      # Add header as key/value pair to attachment part
      part.add_header(
          "Content-Disposition",
          f"attachment; filename= {filename}",
      )

      # Add attachment to message and convert message to string
      message.attach(part)
   
   text = message.as_string()

   # Log in to server using secure context and send email
   context = ssl.create_default_context()
   with smtplib.SMTP(smtp_host, smtp_port) as server:
       server.ehlo()
       server.starttls()
       server.ehlo()
       server.login(sender_email, password)
       server.sendmail(sender_email, receiver_email, text)
       server.quit()
       print('Mail Sent')
   return True
