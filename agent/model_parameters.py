"""One executable parameter contract for routing and both execution paths."""
import math
import os

PARAMETERS = {'max_tokens', 'temperature', 'top_p', 'reasoning_effort'}
EFFORTS = {'low', 'medium', 'high', 'xhigh', 'max'}


def output_limit():
    limit = int(os.environ.get('MAX_OUTPUT_TOKENS', '8192'))
    if not 1 <= limit <= 200000:
        raise ValueError('MAX_OUTPUT_TOKENS is outside the gateway bounds')
    return limit


def validate_parameters(parameters, *, maximum=None):
    unknown = set(parameters) - PARAMETERS
    if unknown:
        raise ValueError('Gateway cannot execute parameters: ' + ', '.join(sorted(unknown)))
    result = {}
    for name, value in parameters.items():
        if value is None:
            continue
        if name == 'reasoning_effort':
            if not isinstance(value, str) or value not in EFFORTS:
                raise ValueError('reasoning_effort must be low, medium, high, xhigh, or max')
            result[name] = value
        elif name == 'max_tokens':
            limit = output_limit() if maximum is None else maximum
            try:
                number = int(value)
            except (TypeError, ValueError, OverflowError):
                raise ValueError('max_tokens must be an integer') from None
            if isinstance(value, bool) or isinstance(value, float) and not value.is_integer():
                raise ValueError('max_tokens must be an integer')
            if not 1 <= number <= limit:
                raise ValueError(f'max_tokens must be between 1 and {limit}')
            result[name] = number
        else:
            try:
                number = float(value)
            except (TypeError, ValueError, OverflowError):
                raise ValueError(f'{name} must be a number') from None
            upper = 2 if name == 'temperature' else 1
            if isinstance(value, bool) or not math.isfinite(number) or not 0 <= number <= upper:
                raise ValueError(f'{name} must be between 0 and {upper}')
            result[name] = number
    return result
