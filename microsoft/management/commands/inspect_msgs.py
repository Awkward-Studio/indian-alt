import os
import extract_msg
from django.core.management.base import BaseCommand

class Command(BaseCommand):
    help = 'Inspect all .msg files in a directory'

    def add_arguments(self, parser):
        parser.add_argument('path', type=str, help='Path to .msg file or directory')

    def handle(self, *args, **options):
        path = options['path']
        
        if os.path.isfile(path):
            files = [path]
        else:
            files = [os.path.join(path, f) for f in os.listdir(path) if f.endswith('.msg')]

        for file_path in files:
            self.stdout.write(f"\nINSPECTING: {file_path}")
            try:
                msg = extract_msg.Message(file_path)
                self.stdout.write(f"  Subject: {msg.subject}")
                self.stdout.write(f"  Sender:  {msg.sender}")
                self.stdout.write(f"  Date:    {msg.date}")
                
                body_len = len(msg.body) if msg.body else 0
                self.stdout.write(f"  Body Length: {body_len} chars")
                
                if msg.body:
                    if "From:" in msg.body or "Sent:" in msg.body or "-----Original Message-----" in msg.body:
                        self.stdout.write("  [Thread detected in body text]")
                    else:
                        self.stdout.write("  [Single message detected]")

                self.stdout.write(f"  Attachments: {len(msg.attachments)}")
                for a in msg.attachments:
                    name = getattr(a, 'filename', getattr(a, 'longFilename', 'Unknown'))
                    self.stdout.write(f"    - {name}")
                msg.close()
            except Exception as e:
                self.stdout.write(f"  Error: {str(e)}")
