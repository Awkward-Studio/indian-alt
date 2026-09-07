"""Settings used by the complete Django test suite."""

from copy import deepcopy

from .local import *  # noqa: F403


# The Venture Intelligence sync integration test exercises a real second
# database alias. Keep it isolated from both the local database and Django's
# default test database so source and destination records cannot overlap.
DATABASES["production"] = deepcopy(DATABASES["default"])  # noqa: F405
DATABASES["production"].setdefault("TEST", {})["NAME"] = "test_indian_alt_production"

# Default to False in general test suite; ingestion-specific test cases explicitly use @override_settings(EMAIL_INGESTION_ENABLED=True)
EMAIL_INGESTION_ENABLED = False

