"""HTTPS client for the hosted Telegram Bot API."""

from http.client import HTTPException
import json
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener


class TelegramError(Exception):
    def __init__(self, method: str, code: int | None = None) -> None:
        message = f"{method}: код {code}" if code is not None else f"{method}: результат запроса неизвестен"
        super().__init__(message)
        self.rejected = code in {400, 401, 403, 404, 429}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Telegram:
    def __init__(self, token: str) -> None:
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.opener = build_opener(NoRedirect())

    def call(self, method: str, *, http_timeout: int = 12, **payload: object) -> object:
        request = Request(
            f"{self.base_url}/{method}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with self.opener.open(request, timeout=http_timeout) as response:
                result = json.load(response)
        except HTTPError as error:
            with error:
                try:
                    result = json.load(error)
                except (ValueError, OSError, HTTPException):
                    raise TelegramError(method) from None
        except (ValueError, OSError, HTTPException):
            raise TelegramError(method) from None

        if isinstance(result, dict):
            if result.get("ok") is True and "result" in result:
                return result["result"]
            if result.get("ok") is False and type(result.get("error_code")) is int:
                raise TelegramError(method, result["error_code"])
        raise TelegramError(method)
