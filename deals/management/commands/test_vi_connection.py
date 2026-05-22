import json
import requests
from django.core.management.base import BaseCommand
from django.conf import settings

class Command(BaseCommand):
    help = "Test connection, authentication, and responses from the Venture Intelligence Commercial API"

    def add_arguments(self, parser):
        parser.add_argument(
            "--key",
            type=str,
            help="Venture Intelligence API Key (overrides settings.VENTURE_INTELLIGENCE_API_KEY)"
        )
        parser.add_argument(
            "--url",
            type=str,
            help="Venture Intelligence Base URL (overrides settings.VENTURE_INTELLIGENCE_BASE_URL)"
        )
        parser.add_argument(
            "--company",
            type=str,
            default="Flipkart",
            help="Company name to search (default: Flipkart)"
        )
        parser.add_argument(
            "--cin",
            type=str,
            help="CIN of company to search (takes precedence over --company)"
        )
        parser.add_argument(
            "--entity",
            type=str,
            help="Entity name of company to search"
        )

    def mask_key(self, key: str) -> str:
        if not key:
            return "None/Empty"
        if len(key) <= 8:
            return "***"
        return f"{key[:3]}...{key[-3:]}"

    def handle(self, *args, **options):
        # 1. Resolve API key and Base URL
        api_key = options["key"] or getattr(settings, "VENTURE_INTELLIGENCE_API_KEY", "")
        base_url = options["url"] or getattr(settings, "VENTURE_INTELLIGENCE_BASE_URL", "https://api-hub.ventureintelligence.com")

        # 2. Resolve query parameters
        query = options["company"]
        query_type = "company_name"
        
        if options["cin"]:
            query = options["cin"]
            query_type = "cin"
        elif options["entity"]:
            query = options["entity"]
            query_type = "entity_name"

        self.stdout.write(self.style.MIGRATE_HEADING("=" * 60))
        self.stdout.write(self.style.MIGRATE_HEADING("Starting Venture Intelligence Connection Test"))
        self.stdout.write(self.style.MIGRATE_HEADING("=" * 60))
        self.stdout.write(f"Target URL: {base_url.rstrip('/')}/vendor/company-full/")
        self.stdout.write(f"Using API Key: {self.mask_key(api_key)}")
        self.stdout.write(f"Query Parameter: {query} ({query_type})")
        self.stdout.write("-" * 60)

        if not api_key:
            self.stdout.write(self.style.ERROR("ERROR: API Key is missing or empty. Please configure VENTURE_INTELLIGENCE_API_KEY in your settings/.env file."))
            self.stdout.write(self.style.MIGRATE_HEADING("=" * 60))
            return

        url = f"{base_url.rstrip('/')}/vendor/company-full/"
        headers = {
            "x-api-key": api_key,
            "Content-Type": "application/json"
        }
        
        payload = {}
        if query_type == "cin":
            payload["cin"] = query
        elif query_type == "company_name":
            payload["company_name"] = query
        elif query_type == "entity_name":
            payload["entity_name"] = query

        self.stdout.write(f"Request Payload: {json.dumps(payload)}")
        self.stdout.write("Sending POST request to Venture Intelligence API...")

        try:
            response = requests.post(url, headers=headers, json=payload, timeout=30)
            
            self.stdout.write(f"Response Status Code: {response.status_code}")
            self.stdout.write(f"Elapsed Time: {response.elapsed.total_seconds():.3f}s")
            self.stdout.write("-" * 60)

            if response.status_code == 200:
                self.stdout.write(self.style.SUCCESS("SUCCESS: Successfully connected to Venture Intelligence API!"))
                try:
                    data = response.json()
                    results = data.get("results", {})
                    profile = results.get("profile", {})
                    if profile:
                        self.stdout.write(self.style.SUCCESS("-" * 40))
                        self.stdout.write(self.style.SUCCESS("Retrieved Company Profile Details:"))
                        self.stdout.write(f"  Name: {profile.get('name')}")
                        self.stdout.write(f"  Registered Name: {profile.get('registered_name')}")
                        self.stdout.write(f"  CIN: {profile.get('cin')}")
                        self.stdout.write(f"  Industry: {profile.get('industry')}")
                        self.stdout.write(f"  Sector: {profile.get('sector')}")
                        self.stdout.write(f"  Total Funding: {profile.get('total_funding')}")
                        self.stdout.write(self.style.SUCCESS("-" * 40))
                    else:
                        self.stdout.write(self.style.WARNING("Response success, but no company profile data was found."))
                    
                    pl_count = len(results.get("profit_loss", [])) if results.get("profit_loss") else 0
                    bs_count = len(results.get("balance_sheet", [])) if results.get("balance_sheet") else 0
                    cf_count = len(results.get("cash_flow", [])) if results.get("cash_flow") else 0
                    self.stdout.write(f"Statements Found: P&L ({pl_count}), Balance Sheet ({bs_count}), Cash Flow ({cf_count})")
                    
                except json.JSONDecodeError:
                    self.stdout.write(self.style.ERROR("Failed to parse response body as JSON."))
                    self.stdout.write(f"Raw Response Body: {response.text}")
                
            elif response.status_code == 404:
                self.stdout.write(self.style.WARNING("NOT FOUND (404): No company matched the search criteria."))
                self.stdout.write(f"Response: {response.text}")
                
            elif response.status_code == 401 or response.status_code == 403:
                self.stdout.write(self.style.ERROR(f"AUTHENTICATION FAILURE ({response.status_code}): Invalid or inactive API key."))
                self.stdout.write(f"Response: {response.text}")
                
            else:
                self.stdout.write(self.style.ERROR(f"HTTP ERROR ({response.status_code}): Server returned an unexpected status code."))
                self.stdout.write(f"Response: {response.text}")

        except requests.exceptions.Timeout:
            self.stdout.write(self.style.ERROR("CONNECTION ERROR: Request to Venture Intelligence API timed out after 30 seconds."))
        except requests.exceptions.RequestException as e:
            self.stdout.write(self.style.ERROR(f"CONNECTION ERROR: An exception occurred while making request: {e}"))
        
        self.stdout.write(self.style.MIGRATE_HEADING("=" * 60))
