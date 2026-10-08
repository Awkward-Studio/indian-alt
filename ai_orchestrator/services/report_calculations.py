"""Check explicit report calculations without relying on the language model's arithmetic."""
import math
import re
import ast
from decimal import Decimal

NUMBER = r"[-+]?\d[\d,]*(?:\.\d+)?"
RATIO = re.compile(rf"(?P<a>{NUMBER})\s*/\s*(?P<b>{NUMBER})\s*=\s*(?P<result>{NUMBER})\s*(?P<unit>x|times|%)?", re.I)
EQUATION = re.compile(rf"(?P<expression>[(-]*{NUMBER}(?:\s*(?:\*\*|[+*/×÷−-])\s*[(+-]*{NUMBER}\s*\)*)+)\s*=\s*(?P<result>{NUMBER})\s*(?P<unit>x|times|%)?", re.I)


def arithmetic_bounds(expression):
    """Propagate displayed input precision through bounded numeric arithmetic."""
    expression = expression.replace(',', '').replace('×', '*').replace('÷', '/').replace('−', '-')
    tree = ast.parse(expression, mode='eval')
    if len(list(ast.walk(tree))) > 64:
        raise ValueError('Expression too complex')

    def bounds(node):
        if isinstance(node, ast.Expression): return bounds(node.body)
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            literal = ast.get_source_segment(expression, node)
            value = numeric(literal)
            error = tolerance(literal) if '.' in literal else Decimal(0)
            return value-error, value+error
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            low, high = bounds(node.operand)
            return (-high, -low) if isinstance(node.op, ast.USub) else (low, high)
        if not isinstance(node, ast.BinOp): raise ValueError('Unsupported expression')
        left, right = bounds(node.left), bounds(node.right)
        if isinstance(node.op, ast.Add): return left[0]+right[0], left[1]+right[1]
        if isinstance(node.op, ast.Sub): return left[0]-right[1], left[1]-right[0]
        if isinstance(node.op, ast.Mult): values = [a*b for a in left for b in right]
        elif isinstance(node.op, ast.Div):
            if right[0] <= 0 <= right[1]: raise ValueError('Denominator includes zero')
            values = [a/b for a in left for b in right]
        elif isinstance(node.op, ast.Pow) and left[0] > 0 and max(abs(x) for x in right) <= 100:
            values = [a**b for a in left for b in right]
        else: raise ValueError('Unsupported expression')
        if not all(value.is_finite() and abs(value) <= Decimal('1e30') for value in values):
            raise ValueError('Result outside arithmetic range')
        return min(values), max(values)
    return bounds(tree)


def numeric(value):
    return Decimal(value.replace(",", ""))


def tolerance(value):
    return Decimal("0.5") * Decimal(10) ** -(len(value.split(".")[1]) if "." in value else 0)


def ratio_bounds(a, b):
    """Propagate the stated decimal precision of rounded display operands."""
    left, right = numeric(a), numeric(b)
    left_error = tolerance(a) if '.' in a else Decimal(0)
    right_error = tolerance(b) if '.' in b else Decimal(0)
    if right-right_error <= 0 <= right+right_error:
        return None
    candidates = [(left+da)/(right+db) for da in (-left_error, left_error) for db in (-right_error, right_error)]
    return min(candidates), max(candidates)


def within_display_precision(value, bounds):
    margin = tolerance(value) + Decimal('0.000001')
    actual = numeric(value)
    return actual+margin >= bounds[0] and actual-margin <= bounds[1]


def report_calculation_errors(markdown: str) -> list[str]:
    errors = []
    source = re.split(r"^###\s+Citations\s*$", markdown, flags=re.M)[0]
    # Currency labels and inline source markers are annotations, not operands.
    arithmetic_source = re.sub(r'\[(?:R\d+[^\]\n]*|\d+(?:\s*[,;]\s*\d+)*)\]', '', source)
    arithmetic_source = re.sub(r'(?:₹|\$|\bINR\b|\bUSD\b)\s*(?=[-+]?\d)', '', arithmetic_source)
    # Report authors use mathematical ^ for powers. Parse the full expression
    # as exponentiation rather than validating only its trailing (1/n) - 1.
    arithmetic_source = arithmetic_source.replace('^', '**')
    # Percent stakes are operands, not result-unit annotations. Preserve the
    # multiplier so a proceeds equation is not checked as its inner EV bridge.
    arithmetic_source = re.sub(rf'(?P<stake>{NUMBER})\s*%\s*(?=[×*÷/+(−-])',
        lambda match: '(' + match['stake'] + ' / 100)', arithmetic_source)
    for match in EQUATION.finditer(arithmetic_source):
        # Simple ratios below also support scenario/IRR consistency checks.
        if RATIO.fullmatch(match[0]): continue
        try:
            bounds = arithmetic_bounds(match['expression'])
        except (ValueError, SyntaxError, ArithmeticError):
            continue
        if match['unit'] == '%' and not re.search(r'(?:\*\s*100\b|\b100\s*\*)', match['expression']):
            bounds = tuple(value*100 for value in bounds)
        if not within_display_precision(match['result'], bounds):
            errors.append(f"{match[0]} falls outside its displayed-input calculation range {bounds[0]:.6f} to {bounds[1]:.6f}{match['unit'] or ''}")
    for paragraph in re.split(r"\n\s*\n", arithmetic_source):
        ratios = []
        for match in RATIO.finditer(paragraph):
            denominator = numeric(match['b'])
            if denominator == 0:
                errors.append(f"Undefined division by zero: {match[0]}")
                continue
            expected = numeric(match['a']) / denominator
            if match['unit'] == '%': expected *= 100
            bounds = ratio_bounds(match['a'], match['b'])
            if bounds and match['unit'] == '%': bounds = tuple(value*100 for value in bounds)
            if bounds and not within_display_precision(match['result'], bounds):
                errors.append(f"{match[0]} should equal {expected:.6f}{match['unit'] or ''}")
            if match['unit'] and match['unit'].lower() in {'x','times'}:
                ratios.append(bounds)
        irr = re.search(rf"(?:Gross\s+)?IRR\s+(?:is\s+)?(?:approximately\s+|about\s+)?(?P<rate>{NUMBER})\s*%\s+over\s+(?P<years>\d+(?:\.\d+)?)\s+years?", paragraph, re.I)
        if irr and len(ratios) == 1 and ratios[0] and ratios[0][0] > 0 and float(irr['years']) > 0:
            years = numeric(irr['years'])
            year_error = tolerance(irr['years']) if '.' in irr['years'] else Decimal(0)
            if years-year_error <= 0: continue
            values = [100*(float(moic)**(1/float(period))-1) for moic in ratios[0]
                      for period in (years-year_error, years+year_error)]
            if all(math.isfinite(value) for value in values) and not within_display_precision(
                    irr['rate'], (Decimal(str(min(values))), Decimal(str(max(values))))):
                errors.append(f"IRR over {irr['years']} years falls outside {min(values):.4f}% to {max(values):.4f}%, not {irr['rate']}%")
    # A scenario table and its own explicitly calculated paragraph must agree.
    calculated_cases = {}
    for match in re.finditer(r"^#{3,4}\s+(.+)\n([\s\S]*?)(?=^#{1,4}\s|\Z)", source, re.M):
        ratios = [m for m in RATIO.finditer(match[2]) if (m['unit'] or '').lower() in {'x','times'} and numeric(m['b'])]
        if len(ratios) == 1:
            calculated_cases[re.sub(r"[^a-z0-9]", "", match[1].casefold())] = ratio_bounds(ratios[0]['a'], ratios[0]['b'])
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
            if value and expected is not None and not within_display_precision(value[1], expected):
                errors.append(f"{cells[0]} table MOIC {cells[column]} conflicts with its cited-input calculation range {expected[0]:.6f}–{expected[1]:.6f}x")
    return errors
