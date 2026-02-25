import os
import extract_msg
from django.core.management.base import BaseCommand

class Command(BaseCommand):
    help = 'Deep inspection of .msg file structure'

    def handle(self, *args, **options):
        file_path = "../Investment Opportunity in Potful _ INR 50 crores.msg"
        if not os.path.exists(file_path):
            file_path = "./Investment Opportunity in Potful _ INR 50 crores.msg"

        msg = extract_msg.Message(file_path)
        
        self.stdout.write(f"SUBJECT: {msg.subject}")
        self.stdout.write(f"SENDER: {msg.sender}")
        self.stdout.write(f"DATE: {msg.date}")
        
        self.stdout.write("\n--- PLAIN BODY ---")
        self.stdout.write(msg.body if msg.body else "None")
        
        self.stdout.write("\n--- ATTACHMENTS ---")
        for i, a in enumerate(msg.attachments):
            name = getattr(a, 'filename', getattr(a, 'longFilename', 'Unknown'))
            self.stdout.write(f"{i+1}. {name} (Type: {type(a)})")
            
        msg.close()
