from django.db import migrations

def update_deal_extraction_skill(apps, schema_editor):
    AISkill = apps.get_model('ai_orchestrator', 'AISkill')
    
    new_output_schema = {
        "deal_model_data": {
            "title": "string",
            "industry": "string",
            "sector": "string",
            "funding_ask": "string",
            "funding_ask_for": "string",
            "priority": "string",
            "city": "string",
            "themes": ["string"]
        },
        "contact_discovery": {
            "firm_name": "string",
            "firm_domain": "string",
            "name": "string",
            "designation": "string",
            "linkedin": "string",
            "email": "string"
        },
        "metadata": {
            "ambiguous_points": ["string"],
            "sources_cited": ["string"]
        },
        "analyst_report": "string"
    }

    new_prompt_template = """Analyze the provided documents and extract deal signals using the Forensic PE Analyst framework.

### INPUT DATA:
{{ content }}

### STRUCTURED OUTPUT REQUIREMENTS:
1. Executive Summary: Verdict (Buy/Hold/Pass) + Top 3 reasons.
2. Strategic Fit & Market Opportunity.
3. Operational Due Diligence.
4. Financial Deep Dive.
5. Risk Matrix (Top 5 risks).
6. Valuation & Exit range.
7. Red Flags & Warning Signs.
8. Next Steps / Data Requests.

Return exactly one valid JSON object and nothing else. Do not use markdown fences or <json> tags. Use these keys:
{
  "deal_model_data": {
    "title": "Exact Company Name",
    "industry": "Industry Name",
    "sector": "Sub-sector",
    "funding_ask": "Numerical value in INR Cr as a string",
    "funding_ask_for": "Use of funds (e.g. Working Capital, Expansion)",
    "priority": "High/Medium/Low based on Phase 4 results",
    "city": "HQ City",
    "themes": ["List of India Alternative themes"]
  },
  "contact_discovery": {
    "firm_name": "Investment Bank or Advisory Firm Name",
    "firm_domain": "Website domain of the firm (e.g. website.com)",
    "name": "Primary Banker or Contact Name",
    "designation": "Designation/Title of the Contact",
    "linkedin": "LinkedIn URL if available",
    "email": "Email address of the contact if available"
  },
  "metadata": {
    "ambiguous_points": ["Specific risks or gaps needing verification"],
    "sources_cited": ["Exact filename where data was found"]
  },
  "analyst_report": "Your full formatted markdown narrative from sections 1-8"
}

RULES:
- THEMES: Every deal MUST be linked to one or more of the following India Alternative themes ONLY. Do not use any other themes:
  a. Women Oriented Consumption
  b. Health & Wellness
  c. Financial Services + Tech
  d. Gen Z + Millennials
  e. Climate & Sustainability
- BANKERS: Investment Banker and advisory team details are typically located on the final pages of the pitch deck or teaser. Extract them carefully into the 'contact_discovery' object. If none are found, output null for contact_discovery.
- Do not repeat the JSON object or any keys.
- Keep 'analyst_report' concise enough to fit in a single response.
- Every claim in 'analyst_report' must include citations [Source: DocName]."""

    AISkill.objects.filter(name="deal_extraction").update(
        output_schema=new_output_schema,
        prompt_template=new_prompt_template
    )

class Migration(migrations.Migration):

    dependencies = [
        ('ai_orchestrator', '0016_rename_ai_orchestr_flow_id_e2f9b2_idx_ai_orchestr_flow_id_b6b5e9_idx_and_more'),
    ]

    operations = [
        migrations.RunPython(update_deal_extraction_skill),
    ]
