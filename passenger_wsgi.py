"""
Entry point cPanel's "Setup Python App" (Phusion Passenger) looks for.

Passenger is supposed to auto-detect an async ASGI callable named
`application` and invoke it correctly. On this host, that detection/
invocation does not work reliably -- confirmed via direct testing: a
minimal `async def application(scope, receive, send)` consistently causes
"Incomplete response received from application", while an equivalent
plain synchronous WSGI `application(environ, start_response)` works
perfectly every time. Passenger here is clearly calling `application` the
WSGI way regardless of whether it's a coroutine function.

Fix: wrap the FastAPI (ASGI) app in a2wsgi's ASGIMiddleware, so the object
actually named `application` is a normal synchronous WSGI callable that
runs our async app to completion internally and returns a plain WSGI
response. `pip install a2wsgi` in this app's virtualenv is required.

The prefix-stripping step (see KNOWN_PREFIXES) still happens first, at the
ASGI level, before the WSGI bridge -- so cPanel's PassengerBaseURI
sub-path mounting keeps working regardless of whether Passenger strips it.
"""
from a2wsgi import ASGIMiddleware

from fresh_api import app as fastapi_app

KNOWN_PREFIXES = ["/apidata", "/status_api", "/tender247_status_api"]


async def _prefix_stripping_asgi_app(scope, receive, send):
    if scope["type"] == "http":
        path = scope["path"]
        for prefix in KNOWN_PREFIXES:
            if path == prefix or path.startswith(prefix + "/"):
                scope = dict(scope)
                scope["path"] = path[len(prefix):] or "/"
                scope["root_path"] = prefix
                break
    await fastapi_app(scope, receive, send)


application = ASGIMiddleware(_prefix_stripping_asgi_app)
