"""Exact loopback origin boundary, independent of desktop TEST policy."""


def browser_context(request):
    return {"local_browser": True}


def csrf_failure(request, reason=""):
    from django.http import HttpResponseForbidden

    return HttpResponseForbidden("Zahteva ni dovoljena. Ponovno odprite obrazec.")


class LocalGateway:
    def __init__(self, application, port):
        self.application = application
        self.authority = f"127.0.0.1:{port}"

    def __call__(self, environ, start_response):
        if (environ.get("REMOTE_ADDR") != "127.0.0.1"
                or environ.get("HTTP_HOST") != self.authority
                or environ.get("HTTP_ORIGIN", "http://" + self.authority) != "http://" + self.authority
                or environ.get("HTTP_SEC_FETCH_SITE", "none") not in {"none", "same-origin"}):
            start_response("403 Forbidden", [("Content-Type", "text/plain"), ("Cache-Control", "no-store")])
            return [b"Request denied."]

        def response(status, headers, exc_info=None):
            headers = [(key, value) for key, value in headers if key.lower() not in {
                "cache-control", "referrer-policy", "content-security-policy",
                "access-control-allow-origin",
            }]
            headers.extend([
                ("Cache-Control", "no-store"), ("Referrer-Policy", "same-origin"),
                ("Content-Security-Policy", ("default-src 'self'; style-src 'self' 'unsafe-inline'; "
                 "img-src 'self' data:; font-src 'self'; connect-src 'self'; "
                 "frame-ancestors 'none'; form-action 'self'; base-uri 'none'")),
            ])
            return start_response(status, headers, exc_info)

        return self.application(environ, response)
