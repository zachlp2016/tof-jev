class AdapterError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 422, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.retryable = retryable


def error_body(exc, request_id: str) -> dict:
    return {
        "error": {"code": exc.code, "message": exc.message, "retryable": exc.retryable},
        "request_id": request_id,
    }
