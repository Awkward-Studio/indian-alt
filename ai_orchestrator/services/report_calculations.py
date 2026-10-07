"""Check explicit report calculations without relying on the language model's arithmetic."""
import math
import re
from decimal import Decimal

NUMBER = r"[-+]?\d[\d,]*(?:\.\d+)?"
RATIO = re.compile(rf"(?P<a>{NUMBER})\s*/\s*(?P<b>{NUMBER})\s*=\s*(?P<result>{NUMBER})\s*(?P<unit>x|times|%)?", re.I)


def numeric(value):
    return Decimal(value.replace(",", ""))


def tolerance(value):
    return Decimal("0.5") * Decimal(10) ** -(len(value.split(".")[1]) if "." in value else 0)


def report_calculation_errors(markdown: str) -> list[str]:
    errors = []
    source = re.split(r"^###\s+Citations\s*$", markdown, flags=re.M)[0]
    for paragraph in re.split(r"\n\s*\n", source):
        ratios = []
        for match in RATIO.finditer(paragraph):
            denominator = numeric(match['b'])
            if denominator == 0:
                errors.append(f"Undefined division by zero: {match[0]}")
                continue
            expected = numeric(match['a']) / denominator
            if match['unit'] == '%': expected *= 100
            if abs(numeric(match['result']) - expected) > tolerance(match['result']) + Decimal('0.000001'):
                errors.append(f"{match[0]} should equal {expected:.6f}{match['unit'] or ''}")
            if match['unit'] and match['unit'].lower() in {'x','times'}:
                ratios.append(float(expected))
        irr = re.search(rf"(?:Gross\s+)?IRR\s+(?:is\s+)?(?:approximately\s+|about\s+)?(?P<rate>{NUMBER})\s*%\s+over\s+(?P<years>\d+(?:\.\d+)?)\s+years?", paragraph, re.I)
        if irr and len(ratios) == 1 and ratios[0] > 0 and float(irr['years']) > 0:
            expected = 100 * (ratios[0] ** (1 / float(irr['years'])) - 1)
            if math.isfinite(expected) and abs(float(numeric(irr['rate'])) - expected) > float(tolerance(irr['rate'])) + .01:
                errors.append(f"IRR for {ratios[0]:.6f}x over {irr['years']} years is {expected:.4f}%, not {irr['rate']}%")
    # A scenario table and its own explicitly calculated paragraph must agree.
    calculated_cases = {}
    for match in re.finditer(r"^#{3,4}\s+(.+)\n([\s\S]*?)(?=^#{1,4}\s|\Z)", source, re.M):
        ratios = [m for m in RATIO.finditer(match[2]) if (m['unit'] or '').lower() in {'x','times'} and numeric(m['b'])]
        if len(ratios) == 1:
            calculated_cases[re.sub(r"[^a-z0-9]", "", match[1].casefold())] = numeric(ratios[0]['a']) / numeric(ratios[0]['b'])
    lines = source.splitlines()
    for index,line in enumerate(lines):
        if not line.strip().startswith('|'): continue
        header = [re.sub(r"[*_`\[\]]", "", c).strip() for c in line.strip().strip('|').split('|')]
        columns = [i for i,label in enumerate(header) if re.search(r"\bMOIC\b",label,re.I)]
        if len(columns) != 1: continue
        column=columns[0]
        for row in lines[index+2:]:
            if not row.strip().startswith('|'): break
            cells=[c.strip() for c in row.strip().strip('|').split('|')]
            if len(cells) != len(header): continue
            key=re.sub(r"[^a-z0-9]", "", cells[0].casefold())
            value=re.fullmatch(rf"\*{{0,2}}({NUMBER})\s*x\*{{0,2}}(?:\s*\[\d+\])*",cells[column],re.I)
            expected=calculated_cases.get(key)
            if value and expected is not None and abs(numeric(value[1])-expected)>tolerance(value[1])+Decimal('0.000001'):
                errors.append(f"{cells[0]} table MOIC {cells[column]} conflicts with its cited-input calculation {expected:.6f}x")
    return errors
