"""Presentation contract transcribed from the supplied 3. PPT - 3.pptx."""
import re

EXECUTIVE_SUMMARY_HEADINGS = (
    'Business overview', 'Funding', 'Promoter & key management', 'Customer traction',
    'Key financials & metrics', 'Investment rationale', 'Key risks & mitigants',
    'Competitor benchmarking - products, financials and valuation',
    'Previous funding rounds & valuation', 'Industry dynamics',
    'Key questions for founders', 'Key takeaways from calls',
    'Similar companies we have seen in the space',
)
TABLE_HEADERS = {
    'Customer traction': ('Metric', 'Value', 'Period / source'),
    'Key risks & mitigants': ('Risk', 'Impact', 'Mitigant / diligence check'),
    'Competitor benchmarking - products, financials and valuation': (
        'Company', 'Type, founded', 'Key products', 'Hero price point', 'Differentiation',
        'FY', 'Revenue', 'YoY growth', 'Gross margin', 'EBITDA margin', 'Total raised',
        'Key investors', 'Last post-money (date)', 'EV / Rev.', 'EV / EBITDA'),
    'Previous funding rounds & valuation': (
        'Round', 'Date', 'Lead / key investors', 'Raised', 'Post-money equity', 'Net cash',
        'EV', 'Ref. FY', 'Revenue', 'Gross margin', 'EBITDA margin', 'EV / Revenue',
        'EV / Gross profit', 'EV / EBITDA'),
    'Key questions for founders': ('Theme', 'Question to ask', 'What good looks like'),
    'Key takeaways from calls': ('Spoke with', 'Type', 'Date', 'Key takeaway'),
    'Similar companies we have seen in the space': ('Company', 'Category', 'Seen / stage', 'Our view'),
}
SUBTABLE_HEADERS = {
    'Key metrics': ('Metric', 'Value', 'Metric', 'Value'),
    'Market size by sub-sector': ('Sub-sector', 'Market size'),
    'Market sizing': ('Metric', 'Value', 'Scope / period'),
    'Sub-sector dynamics': ('Sub-sector', 'CAGR', 'Online share', 'Growth driver', 'Company play'),
}
EXECUTIVE_SUMMARY_REPORT_BASIS = '''Executive summary report sources v1:
This is a summary of the ten other report sections. Their saved findings, numbers,
units, fiscal periods, forecast status, risks and conclusions govern each summary block.
Prefer the completed section from this run; otherwise use its latest saved report version.
Do not independently substitute figures from another workbook, a partial period, an
older draft or unrelated retrieval evidence. Keep reported contradictions and gaps.
Required source mapping:
Business overview and Customer traction: Company Details.
Funding: Transaction Details.
Promoter & key management: Promoter and Management Details.
Key financials & metrics: Key Financials, including chart data and key metric values.
Investment rationale: Investment Rationale.
Key risks & mitigants: Risk Factors.
Competitor benchmarking: Industry Overview and Transaction / Trading Multiples.
Previous funding rounds & valuation: Transaction Details and Transaction / Trading Multiples.
Industry dynamics: Industry Overview.
Key questions for founders: Next Steps and the unresolved gates in Risk Factors.
Key takeaways from calls: actual recorded calls summarized in Company Details,
Promoter and Management Details or Industry Overview; otherwise Not provided.
Similar companies we have seen: internal deal comparisons in Industry Overview and
the supplied industry/sector plus semantic internal-deal records. Use their real stage,
received date and recorded team view; never invent a passed or invested decision.
Exit Considerations must inform the exit conditions and decision in Investment rationale.
Recent saved competitor search and profile evidence may supplement the competition
blocks, and current internal-deal retrieval may supply similar-company rows. Identify
any conflict with the saved reports rather than silently changing the target's facts.
Primary retrieval blocks supply source provenance and gap checks; they do not replace
the corresponding report section's reporting basis. Preserve all thirteen presentation
headings and every required table. Missing source values remain Not provided.
'''
EXECUTIVE_SUMMARY_FORMAT = '''Executive summary presentation format v1:
Match ALL THREE pages of the supplied "3. PPT - 3.pptx". Keep the top heading
## Executive Summary. Use the following EXACT level-three headings, in this order:
''' + '\n'.join('### '+h for h in EXECUTIVE_SUMMARY_HEADINGS) + '''
Page 1 uses Business overview, Funding, Promoter & key management, Customer traction,
Key financials & metrics, Investment rationale, and Key risks & mitigants.
Page 2 uses Competitor benchmarking, Previous funding rounds, and Industry dynamics.
Page 3 uses Key questions, Key takeaways from calls, and Similar companies.
Use compact, source-grounded bullets and tables suitable for these three presentation
pages, not an essay or additional sections. These exact layout rules override earlier
summary layout suggestions. Retain recommendation, countercase and approval gates
within Investment rationale and Key risks & mitigants; do not add separate headings.
Business overview: founding, location, ownership, products, business model, channels
and positioning. Funding: total raised, rounds, investors and current proposed round.
Promoter & key management: name/role, ownership, relevant background and responsibilities.
Customer traction: a table of customer, repeat, volume, rating or channel traction.
Investment rationale: approximately six concise, evidence-backed points and a provisional
call with conditions. Risks: approximately four to six decision-critical risks.
Key financials & metrics must contain exactly these level-four subheadings:
#### Financial performance
A table starting | Metric | followed by source fiscal/quarterly/ARR periods, including
actual/forecast labels. Rows: Revenue and EBITDA margin. Label the monetary units
in a short sentence above the table. Use INR Cr only when currency/scale is explicit;
otherwise preserve native units and state unknown dimensions. This table supplies
the presentation's revenue bars and EBITDA-margin line; never plot missing data as zero.
#### Key metrics
A four-column table | Metric | Value | Metric | Value |. Four rows pair receivable days
with gross margin; inventory days with net profit / loss; payable days with cash balance;
and cash conversion cycle with a business-specific industry metric. State fiscal periods,
units and citations inside values. Unsupported inputs are Not provided, never guessed.
Industry dynamics must contain exactly these level-four subheadings:
#### Market size by sub-sector
A table | Sub-sector | Market size |, with currency/unit, period, scope and cited values.
#### Market sizing
A table | Metric | Value | Scope / period |, with TAM, SAM, SOM rows. No invented
market shares or mechanically assumed SOM. An analyst scenario must be explicitly labelled.
#### Sub-sector dynamics
A table | Sub-sector | CAGR | Online share | Growth driver | Company play |.
Other required table headers (copy verbatim and in this order):
''' + '\n'.join(h+': | '+' | '.join(cols)+' |' for h,cols in TABLE_HEADERS.items()) + '''
Include the target and relevant, evidenced peers in competitor benchmarking. State
monetary units and valuation date/basis; do not equate funding asks to enterprise value.
EV and multiples need source-backed equity value, net cash, periods and denominators.
Use n.m. for negative EBITDA, Not provided for missing inputs. Keep the current proposed
round distinct from completed funding. Founder questions: about ten decision-linked
questions grounded in deal gaps and risks, with specific observable evidence for a good answer.
Call takeaways may use only actual supplied calls, meeting notes or correspondence;
never invent people, dates, expert calls or reported opinions. Similar companies we have
seen means evidenced INTERNAL prior deals and the team's recorded view, not public peers.
If no calls/internal comparable deals are supplied, retain each required table with
Not provided cells and one concise statement that the deal team must supply records.
All required sections and table columns must remain even when evidence is missing.
Do not reuse Perfora names, sample values, ratings, placeholder multiples or illustrative
market sizes from the reference deck. Cite material factual claims and numeric cells
using the supplied [Rnnn] markers. Keep references in the normal report sources footer.
'''

def executive_summary_output_skeleton():
    lines = ['## Executive Summary']
    for title in EXECUTIVE_SUMMARY_HEADINGS:
        lines.append('### '+title)
        if title == 'Key financials & metrics':
            lines.extend(['#### Financial performance', '#### Key metrics'])
        elif title == 'Industry dynamics':
            lines.extend(['#### Market size by sub-sector', '#### Market sizing', '#### Sub-sector dynamics'])
    return '\n\n'.join(lines)


def executive_summary_format_errors(markdown):
    headings = re.findall(r'^### (.+?)\s*$', markdown, re.M)
    headings = [h for h in headings if h.casefold() not in {'sources', 'sources and references', 'references', 'citations'}]
    errors = []
    if tuple(headings) != EXECUTIVE_SUMMARY_HEADINGS:
        missing = [title for title in EXECUTIVE_SUMMARY_HEADINGS if title not in headings]
        unexpected = [title for title in headings if title not in EXECUTIVE_SUMMARY_HEADINGS]
        errors.append('Executive Summary must use the exact thirteen presentation headings in the required order. '
                      'Missing headings: '+('; '.join(missing) or 'none')+'. '
                      'Unexpected headings: '+('; '.join(unexpected) or 'none')+'. '
                      'Required order: '+'; '.join(EXECUTIVE_SUMMARY_HEADINGS)+'. '
                      'Return all three presentation pages, including the complete Industry dynamics block and all founder, call and internal-comparison tables.')
    subheadings = re.findall(r'^#### (.+?)\s*$', markdown, re.M)
    if subheadings != ['Financial performance', 'Key metrics', 'Market size by sub-sector', 'Market sizing', 'Sub-sector dynamics']:
        errors.append('Use these five financial and industry subheadings in this order: '
                      'Financial performance; Key metrics; Market size by sub-sector; Market sizing; Sub-sector dynamics. '
                      'Observed: '+'; '.join(subheadings)+'.')
    all_sections = {}
    for level in (3, 4):
        matches = list(re.finditer(r'^'+('#' * level)+r' (.+?)\s*$', markdown, re.M))
        for match in matches:
            end = re.search(r'^#{2,'+str(level)+r'} ', markdown[match.end():], re.M)
            all_sections[match[1]] = markdown[match.end():match.end()+end.start() if end else len(markdown)]
    def cells(line):
        return [re.sub(r'\[(?:R\d+|\d+)(?:@[^\]]+)?\]|[*_`]', '', c).strip()
                for c in re.split(r'(?<!\\)\|', line.strip().strip('|'))]
    for title, expected in {**TABLE_HEADERS, **SUBTABLE_HEADERS}.items():
        body = all_sections.get(title, '')
        lines = [line for line in body.splitlines() if line.strip().startswith('|')]
        if len(lines) < 3 or tuple(cells(lines[0])) != expected:
            errors.append(f'{title} must contain a populated table with the exact columns: '+', '.join(expected)+'.')
        elif any(len(cells(line)) != len(expected) for line in lines):
            errors.append(f'{title} contains a table row with missing or extra columns.')
    body = all_sections.get('Financial performance', '')
    rows = [cells(line) for line in body.splitlines() if line.strip().startswith('|')]
    # Models may qualify a metric with its unit or change capitalization. These
    # labels still describe the chart's two axes; do not retry valid tables.
    def metric_label(value):
        return re.sub(r'\s*\([^()]*\)\s*$', '', value).strip().casefold()
    labels = [metric_label(r[0]) for r in rows[2:]]
    if len(rows) < 4 or rows[0][0].casefold() != 'metric' or len(rows[0]) < 2 or labels != ['revenue', 'ebitda margin'] or any(len(r) != len(rows[0]) for r in rows):
        errors.append('Financial performance needs exactly two metric rows in order: Revenue, EBITDA margin. '
                      'Use | Metric | <source period> | as the header and the same column count in every row. '
                      'Capitalization and parenthesized units in metric labels are allowed. '
                      'Observed metric rows: '+', '.join(r[0] for r in rows[2:])+'.')
    return errors
