"""Common application error contract."""


class ApplicationError(Exception):
    code = "application_error"

    def __init__(self) -> None:
        super().__init__(self.code)
