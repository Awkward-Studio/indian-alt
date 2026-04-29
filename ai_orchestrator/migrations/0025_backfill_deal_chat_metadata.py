from django.db import migrations


def backfill_deal_chat_metadata(apps, schema_editor):
    AIConversation = apps.get_model("ai_orchestrator", "AIConversation")
    Deal = apps.get_model("deals", "Deal")

    for conversation in AIConversation.objects.filter(title__startswith="Chat: "):
        title = str(conversation.title or "")
        deal_title = title.replace("Chat: ", "", 1).strip()
        if not deal_title:
            continue
        deal = Deal.objects.filter(title=deal_title).first()
        if not deal:
            continue
        metadata = conversation.metadata or {}
        metadata.update({
            "kind": "deal_chat",
            "deal_id": str(deal.id),
            "deal_title": deal.title,
            "legacy_title": title,
        })
        conversation.metadata = metadata
        conversation.title = f"{deal.title}: Legacy Chat"[:255]
        conversation.save(update_fields=["metadata", "title"])


class Migration(migrations.Migration):

    dependencies = [
        ('ai_orchestrator', '0024_aiconversation_metadata'),
        ('deals', '0027_dealrelationshipcontext'),
    ]

    operations = [
        migrations.RunPython(backfill_deal_chat_metadata, migrations.RunPython.noop),
    ]
