"""Structured failures and bounded JSON validation, extracted from monkey."""
import math


class BeaverError(ValueError):
    def __init__(self, code, location, message, status=None):
        super().__init__(f"{code} at {location}: {message}")
        self.code, self.location, self.message = code, str(location), message
        self.status = status or ('failed' if any(x in code for x in ('_FAILED','TIMEOUT','_READBACK','UNEXPECTED_CHANGE','BUILD_SOURCE_DRIFT')) else 'blocked')
        if self.status not in ('blocked','failed'):raise ValueError('Invalid error status')

    def as_dict(self):
        return dict(code=self.code, location=self.location, message=self.message,status=self.status,
                    retry='new_plan_after_resolving_precondition' if self.status=='blocked' else 'inspect_transaction_before_retry')


def fail(code, location, message, status=None):
    raise BeaverError(code, location, message,status)


def budget(document, max_depth=96, max_nodes=500000):
    pending = [(document, 0)]
    count = 0
    while pending:
        value, depth = pending.pop()
        count += 1
        if isinstance(value, float) and not math.isfinite(value):
            fail("INVALID_NUMBER", "$", "Non-finite number")
        if isinstance(value, str):
            try:
                value.encode("utf-8")
            except UnicodeError:
                fail("INVALID_UNICODE", "$", "Unpaired Unicode surrogate")
        if depth > max_depth or count > max_nodes:
            fail("BUDGET_EXCEEDED", "$", "Depth or value budget exceeded")
        if isinstance(value, list):
            pending.extend((x, depth + 1) for x in value)
        elif isinstance(value, dict):
            pending.extend((x, depth + 1) for pair in value.items() for x in pair)
