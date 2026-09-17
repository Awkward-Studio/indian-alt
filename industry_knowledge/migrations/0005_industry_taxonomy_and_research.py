from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("industry_knowledge", "0004_industry_industrydocument_industrynewsarticle")]

    operations = [
        migrations.AddField(model_name="industry", name="parent", field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="sub_industries", to="industry_knowledge.industry")),
        migrations.AddField(model_name="industry", name="market_size", field=models.CharField(blank=True, default="", max_length=255)),
        migrations.AddField(model_name="industry", name="growth_rate", field=models.CharField(blank=True, default="", max_length=255)),
        migrations.AddField(model_name="industry", name="classification_status", field=models.CharField(default="AUTO_CHECKED", max_length=30)),
        migrations.AddField(model_name="industry", name="classification_basis", field=models.CharField(blank=True, default="", max_length=500)),
        migrations.AddField(model_name="industry", name="research_status", field=models.CharField(choices=[("IDLE", "Ready"), ("QUEUED", "Queued"), ("RUNNING", "Searching"), ("COMPLETE", "Complete"), ("FAILED", "Failed")], default="IDLE", max_length=20)),
        migrations.AddField(model_name="industry", name="last_researched_at", field=models.DateTimeField(blank=True, null=True)),
        migrations.AddField(model_name="industry", name="research_error", field=models.TextField(blank=True, default="")),
        migrations.AddField(model_name="industrynewsarticle", name="category", field=models.CharField(choices=[("TRANSACTION", "Transaction"), ("REPORT", "Web report"), ("NEWS", "News")], default="NEWS", max_length=20)),
    ]
