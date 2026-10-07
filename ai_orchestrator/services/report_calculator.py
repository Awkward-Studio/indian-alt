"""Bounded decimal arithmetic for report generation. No Python evaluation or external access."""
import ast
import json
import re
from decimal import Decimal, localcontext

CALCULATOR_INSTRUCTIONS = '''
Report calculator capability:
Use the calculator for material numerical derivations, unit conversions, growth,
variances, valuation bridges and returns before writing the final report. To request
new arithmetic, first check the supplied primary saved cell facts: reuse their
derived_display_values for the same value, currency, period and scale. These exact
application-computed conversions do not need another calculator request. Batch only
material calculations whose results are not already supplied. To request
calculations, reply ONLY with <report_calculations> followed by a JSON array and
</report_calculations>. Each object has expression, label and source_markers.
For example, expression "(180 / 60) ** (1 / 5) - 1" computes a single-entry/exit
annual return. Use your supplied source inputs, not these example numbers. Supported
operators are +, -, *, /, ** and parentheses. Batch up to 24 calculations per request.
Prioritize the material calculations; do not attempt an exhaustive conversion of every
retrieved number. A request must be a complete JSON array with its closing tag. After
at most three small batches, return the complete final report, not more calculator JSON.
The application returns decimal results. Then write the requested complete Markdown
section with exact evidence markers and no calculator protocol or implementation
details. A calculator result verifies arithmetic, not source authenticity. Preserve
each input's year, units, basis, citations and signs; disclose unsupported assumptions.
If an input is absent, flag the gap rather than inventing an operand. Compare source
values only after aligning units, periods and scope. Different forecasts or reporting
bases require reconciliation, not an automatic contradiction flag.
'''


def calculate(expression: str) -> str:
    if not isinstance(expression, str) or len(expression) > 256:
        raise ValueError('Expression must be at most 256 characters.')
    tree = ast.parse(expression, mode='eval')
    if len(list(ast.walk(tree))) > 64:
        raise ValueError('Expression is too complex.')

    def evaluate(node):
        if isinstance(node, ast.Expression): return evaluate(node.body)
        if isinstance(node, ast.Constant) and type(node.value) in (int,float):
            value=Decimal(str(node.value))
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op,(ast.UAdd,ast.USub)):
            value=evaluate(node.operand) * (-1 if isinstance(node.op,ast.USub) else 1)
        elif isinstance(node, ast.BinOp):
            left,right=evaluate(node.left),evaluate(node.right)
            if isinstance(node.op,ast.Add): value=left+right
            elif isinstance(node.op,ast.Sub): value=left-right
            elif isinstance(node.op,ast.Mult): value=left*right
            elif isinstance(node.op,ast.Div): value=left/right
            elif isinstance(node.op,ast.Pow) and abs(right)<=100: value=left**right
            else: raise ValueError('Unsupported arithmetic operator or exponent.')
        else: raise ValueError('Only numeric literals and arithmetic operators are allowed.')
        if not value.is_finite() or abs(value)>Decimal('1e30'):
            raise ValueError('Calculation is outside the supported numeric range.')
        return value

    with localcontext() as context:
        context.prec=32
        return str(evaluate(tree))


def calculation_request(text: str) -> list[dict] | None:
    match=re.fullmatch(r'\s*<report_calculations>([\s\S]+)</report_calculations>\s*',text)
    if not match:
        if '<report_calculations>' in text or '</report_calculations>' in text:
            raise ValueError('Calculator request must be a complete JSON array of at most 24 calculations with its closing tag.')
        return None
    requests=json.loads(match[1])
    if not isinstance(requests,list) or not 1<=len(requests)<=24:
        raise ValueError('Request between 1 and 24 calculations.')
    results=[]
    for request in requests:
        if not isinstance(request,dict): raise ValueError('Each calculation must be an object.')
        expression=request.get('expression')
        result={'expression':expression,'label':str(request.get('label') or '')[:120],
                'source_markers':request.get('source_markers') if isinstance(request.get('source_markers'),list) else []}
        try: result['result']=calculate(expression)
        except (ValueError,ArithmeticError,SyntaxError) as error: result['error']=str(error)[:180]
        results.append(result)
    return results
