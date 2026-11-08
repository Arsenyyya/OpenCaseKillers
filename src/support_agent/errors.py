class AgentError(Exception):
    code = "operation_failed"
    status = 400


class Forbidden(AgentError):
    code, status = "forbidden", 403


class NotFound(AgentError):
    code, status = "not_found", 404


class Conflict(AgentError):
    code, status = "stale_revision", 409


class Permanent(AgentError):
    code = "permanent_processing_error"


class Transient(AgentError):
    code, status = "provider_unavailable", 503

    def __init__(self, retry_after=0):
        self.retry_after = min(float(retry_after), 3600)


class Capacity(Transient):
    code = "capacity_wait"


class Stale(AgentError):
    code = "stale_claim"


class Budget(Permanent):
    code = "model_budget_exhausted"


class InvalidOutput(Permanent):
    code = "invalid_model_output"


class DeliveryUnknown(AgentError):
    code = "delivery_unknown"
