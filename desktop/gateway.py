"""Per-launch capability mount, exact origin checks, and offline response policy."""

import hmac

POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; font-src 'self'; connect-src 'self'; "
    "frame-ancestors 'none'; form-action 'self'; base-uri 'none'; object-src 'self'"
)


def csrf_failure(request, reason=""):
    from django.http import HttpResponseForbidden

    return HttpResponseForbidden("Zahteva ni dovoljena. Ponovno odprite obrazec.")


class Gateway:
    def __init__(self, application, prefix, authority):
        self.application = application
        self.prefix = prefix
        self.authority = authority

    def __call__(self, environ, start_response):
        origin = "http://" + self.authority
        path = environ.get("PATH_INFO", "")
        if (
            environ.get("REMOTE_ADDR") != "127.0.0.1"
            or environ.get("HTTP_HOST") != self.authority
            or environ.get("HTTP_ORIGIN", origin) != origin
            or environ.get("HTTP_SEC_FETCH_SITE", "none") not in {"none", "same-origin"}
            or not hmac.compare_digest(path[:len(self.prefix)].encode(), self.prefix.encode())
            or not path.startswith(self.prefix + "/")
        ):
            start_response("403 Forbidden", [("Content-Type", "text/plain"), ("Cache-Control", "no-store")])
            return [b"Request denied."]
        environ["SCRIPT_NAME"] = self.prefix
        environ["PATH_INFO"] = path[len(self.prefix):]

        def secure_response(status, headers, exc_info=None):
            headers = [(k, v) for k, v in headers if k.lower() not in {
                "content-security-policy", "referrer-policy", "cache-control",
            }]
            headers.extend([
                ("Content-Security-Policy", POLICY),
                ("Referrer-Policy", "no-referrer"),
                ("Cache-Control", "no-store"),
                ("X-Content-Type-Options", "nosniff"),
            ])
            return start_response(status, headers, exc_info)

        if environ["PATH_INFO"] == "/__ready__":
            secure_response("200 OK", [("Content-Type", "text/plain")])
            return [b"ready"]
        return self.application(environ, secure_response)
