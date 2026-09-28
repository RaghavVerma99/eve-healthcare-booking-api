class AppError(Exception):
    status_code = 500
    code = "internal_error"
    message = "An unexpected error occurred."

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        details: dict | list | None = None,
        status_code: int | None = None,
    ) -> None:
        self.message = message or self.message
        self.code = code or self.code
        self.details = details
        if status_code is not None:
            self.status_code = status_code
        super().__init__(self.message)


class NotFoundError(AppError):
    status_code = 404
    code = "not_found"
    message = "Resource not found."


class ConflictError(AppError):
    status_code = 409
    code = "conflict"
    message = "Resource state conflict."


class UnprocessableError(AppError):
    status_code = 422
    code = "unprocessable"
    message = "Request could not be processed."


class AuthenticationError(AppError):
    status_code = 401
    code = "unauthorized"
    message = "Could not validate credentials."


class PermissionDeniedError(AppError):
    status_code = 403
    code = "forbidden"
    message = "You do not have permission to perform this action."


class ServiceUnavailableError(AppError):
    status_code = 503
    code = "service_unavailable"
    message = "A dependency this request needs is unavailable."


class InvalidStateTransitionError(ConflictError):
    code = "invalid_state_transition"
    message = "The requested state transition is not allowed."
