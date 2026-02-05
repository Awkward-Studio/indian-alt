import os
import sys
import importlib.util
from django.apps import AppConfig


class CoreConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'core'
    
    def ready(self):
        """
        Fix name collision: Local Django app 'requests' shadows HTTP 'requests' library.
        This runs after Django apps are loaded but before URL checks.
        Ensures DRF gets the HTTP requests library, not the Django app.
        """
        # Check if requests is the Django app (it won't have packages attribute)
        if 'requests' in sys.modules:
            requests_module = sys.modules['requests']
            # If it's the Django app, it won't have packages.urllib3
            if not hasattr(requests_module, 'packages') or not hasattr(getattr(requests_module, 'packages', None), 'urllib3'):
                # Find and load HTTP requests from site-packages
                for path in sys.path:
                    if 'site-packages' in path:
                        _requests_path = os.path.join(path, 'requests', '__init__.py')
                        if os.path.exists(_requests_path):
                            spec = importlib.util.spec_from_file_location('requests_http', _requests_path)
                            requests_http = importlib.util.module_from_spec(spec)
                            requests_http.__package__ = 'requests'
                            spec.loader.exec_module(requests_http)
                            
                            # Patch packages attribute for DRF
                            _urllib3_path = os.path.join(path, 'urllib3', '__init__.py')
                            if os.path.exists(_urllib3_path):
                                urllib3_spec = importlib.util.spec_from_file_location('urllib3', _urllib3_path)
                                urllib3_module = importlib.util.module_from_spec(urllib3_spec)
                                urllib3_module.__package__ = 'urllib3'
                                urllib3_spec.loader.exec_module(urllib3_module)
                                
                                if not hasattr(requests_http, 'packages'):
                                    requests_http.packages = type('obj', (object,), {'urllib3': urllib3_module})()
                            
                            # Replace with HTTP requests library
                            sys.modules['requests'] = requests_http
                            break