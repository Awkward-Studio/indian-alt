from django.db import migrations


SOURCES = [
    ("Entrackr", "https://entrackr.com"),
    ("Inc42", "https://inc42.com"),
    ("Venture Intelligence", "https://ventureintelligence.com"),
    ("The Economic Times", "https://economictimes.indiatimes.com"),
]


def seed_sources(apps, schema_editor):
    NewsSource = apps.get_model("industry_knowledge", "NewsSource")
    for name, homepage_url in SOURCES:
        NewsSource.objects.get_or_create(
            name=name,
            defaults={
                "homepage_url": homepage_url,
                "is_active": False,
                "requires_licensed_api": name == "Venture Intelligence",
            },
        )


class Migration(migrations.Migration):
    dependencies = [("industry_knowledge", "0001_initial")]
    operations = [migrations.RunPython(seed_sources, migrations.RunPython.noop)]
