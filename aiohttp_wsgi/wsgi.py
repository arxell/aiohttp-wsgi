"""
Running a WSGI app
==================

.. currentmodule:: aiohttp_wsgi

:mod:`aiohttp_wsgi` allows you to run WSGI applications (e.g. `Django`_, `Flask`_) on :ref:`aiohttp <aiohttp-web>`.
This allows you to add async features like websockets and long-polling to an existing Python web app.

.. hint::

    If you don't need to add :ref:`websockets <aiohttp-web-websockets>` or
    :ref:`async request handlers <aiohttp-web-handler>` to your app, but still want to run your WSGI app on the
    :mod:`asyncio` event loop, :mod:`aiohttp_wsgi` provides a simpler :doc:`command line interface <main>`.


Run a simple web server
-----------------------

In order to implement a WSGI server, first import your WSGI application and wrap it in a :class:`WSGIHandler`.

.. code:: python

    from aiohttp import web
    from aiohttp_wsgi import WSGIHandler
    from your_project.wsgi import application

    wsgi_handler = WSGIHandler(application)


Next, create an :class:`Application <aiohttp.web.Application>` instance and register the request handler with the
application's :class:`router <aiohttp.web.UrlDispatcher>` on a particular HTTP *method* and *path*:

.. code:: python

    app = web.Application()
    app.router.add_route("*", "/{path_info:.*}", wsgi_handler)

After that, run the application by :func:`run_app() <aiohttp.web.run_app>` call:

.. code:: python

    web.run_app(app)

See the :ref:`aiohttp.web <aiohttp-web>` documentation for information on adding
:ref:`websockets <aiohttp-web-websockets>` and :ref:`async request handlers <aiohttp-web-handler>` to your app.


Extra environ keys
------------------

:mod:`aiohttp_wsgi` adds the following additional keys to the WSGI environ:

``asyncio.loop``
    The :class:`EventLoop <asyncio.BaseEventLoop>` running the server.

``asyncio.executor``
    The :class:`Executor <concurrent.futures.Executor>` running the WSGI request.

``aiohttp.request``
    The raw :class:`aiohttp.web.Request` that initiated the WSGI request. Use this to access additional
    request :ref:`metadata <aiohttp-web-data-sharing>`.


API reference
-------------

.. autoclass:: WSGIHandler
    :members:


.. include:: /_include/links.rst
"""

import sys
from asyncio import get_event_loop
from io import BytesIO
from tempfile import TemporaryFile
from wsgiref.util import is_hop_by_hop
from aiohttp.web import Response, HTTPRequestEntityTooLarge
from aiohttp_wsgi.utils import parse_sockname


class ReadBuffer:

    __slots__ = (
        "_inbuf_overflow", "_max_request_body_size", "_body", "_loop", "_executor", "_content_length", "_overflow",
    )

    def __init__(self, inbuf_overflow, max_request_body_size, loop, executor):
        self._inbuf_overflow = inbuf_overflow
        self._max_request_body_size = max_request_body_size
        self._body = BytesIO()
        self._loop = loop
        self._executor = executor
        self._content_length = 0
        self._overflow = False

    async def _run(self, fn, *args):
        if self._overflow:
            return await self._loop.run_in_executor(self._executor, fn, *args)
        else:
            return fn(*args)

    async def write(self, data):
        self._content_length += len(data)
        # Check for body size overflow. The request might be streaming, so we check with every chunk.
        if self._content_length > self._max_request_body_size:
            raise HTTPRequestEntityTooLarge()
        # Overflow onto disk, if required.
        if not self._overflow and self._content_length > self._inbuf_overflow:
            self._overflow = True
            overflow_body = await self._run(TemporaryFile)
            await self._run(overflow_body.write, self._body.getbuffer())
            self._body.close()
            self._body = overflow_body
        # Write the block.
        await self._run(self._body.write, data)

    async def get_body(self):
        await self._run(self._body.seek, 0)
        return self._body, self._content_length

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        await self._run(self._body.close)


def _run_application(application, environ):
    # Simple start_response callable.
    def start_response(status, headers, exc_info=None):
        nonlocal response_status, response_reason, response_headers, response_body
        status_code, reason = status.split(None, 1)
        status_code = int(status_code)
        # Check the headers.
        for header_name, header_value in headers:
            assert not is_hop_by_hop(header_name), "hop-by-hop headers are forbidden"
        # Start the response.
        response_status = status_code
        response_reason = reason
        response_headers = headers
        del response_body[:]
        return response_body.append
    # Response data.
    response_status = None
    response_reason = None
    response_headers = None
    response_body = []
    # Run the application.
    body_iterable = application(environ, start_response)
    try:
        response_body.extend(body_iterable)
        assert response_status is not None, "application did not call start_response()"
        return response_status, response_reason, response_headers, b"".join(response_body)
    finally:
        # Close the body.
        if hasattr(body_iterable, "close"):
            body_iterable.close()


class WSGIHandler:

    """
    An adapter for WSGI applications, allowing them to run on :ref:`aiohttp <aiohttp-web>`.

    :param callable application: {application}
    :param str url_scheme: {url_scheme}
    :param io.BytesIO stderr: {stderr}
    :param int inbuf_overflow: {inbuf_overflow}
    :param int max_request_body_size: {max_request_body_size}
    :param concurrent.futures.Executor executor: {executor}
    :param asyncio.BaseEventLoop loop: {loop}
    """

    def __init__(
        self,
        application,
        *,
        # Handler config.
        url_scheme=None,
        stderr=None,
        inbuf_overflow=524288,
        max_request_body_size=1073741824,
        # asyncio config.
        executor=None,
        loop=None
    ):
        assert callable(application), "application should be callable"
        self._application = application
        # Handler config.
        self._url_scheme = url_scheme
        self._stderr = stderr or sys.stderr
        assert isinstance(inbuf_overflow, int), "inbuf_overflow should be int"
        assert inbuf_overflow >= 0, "inbuf_overflow should be >= 0"
        self._inbuf_overflow = inbuf_overflow
        assert isinstance(max_request_body_size, int), "max_request_body_size should be int"
        assert max_request_body_size >= 0, "max_request_body_size should be >= 0"
        self._max_request_body_size = max_request_body_size
        # asyncio config.
        self._executor = executor
        self._loop = loop or get_event_loop()

    def _get_environ(self, request, body, content_length):
        # Resolve the path info.
        path_info = request.match_info["path_info"]
        script_name = request.rel_url.path[:len(request.rel_url.path)-len(path_info)]
        # Special case: If the app was mounted on the root, then the script name will
        # currently be set to "/", which is illegal in the WSGI spec. The script name
        # could also end with a slash if the WSGIHandler was mounted as a route
        # manually with a trailing slash before the path_info. In either case, we
        # correct this according to the WSGI spec by transferring the trailing slash
        # from script_name to the start of path_info.
        if script_name.endswith("/"):
            script_name = script_name[:-1]
            path_info = "/" + path_info
        # Parse the connection info.
        server_name, server_port = parse_sockname(request.transport.get_extra_info("sockname"))
        remote_addr, remote_port = parse_sockname(request.transport.get_extra_info("peername"))
        # Detect the URL scheme.
        url_scheme = self._url_scheme
        if url_scheme is None:
            url_scheme = "http" if request.transport.get_extra_info("sslcontext") is None else "https"
        # Create the environ.
        environ = {
            "REQUEST_METHOD": request.method,
            "SCRIPT_NAME": script_name,
            "PATH_INFO": path_info,
            "QUERY_STRING": request.rel_url.query_string,
            "CONTENT_TYPE": request.headers.get("Content-Type", ""),
            "CONTENT_LENGTH": str(content_length),
            "SERVER_NAME": server_name,
            "SERVER_PORT": server_port,
            "REMOTE_ADDR": remote_addr,
            "REMOTE_HOST": remote_addr,
            "REMOTE_PORT": remote_port,
            "SERVER_PROTOCOL": "HTTP/{}.{}".format(*request.version),
            "wsgi.version": (1, 0),
            "wsgi.url_scheme": url_scheme,
            "wsgi.input": body,
            "wsgi.errors": self._stderr,
            "wsgi.multithread": True,
            "wsgi.multiprocess": False,
            "wsgi.run_once": False,
            "asyncio.loop": self._loop,
            "asyncio.executor": self._executor,
            "aiohttp.request": request,
        }
        # Add in additional HTTP headers.
        for header_name in request.headers:
            header_name = header_name.upper()
            if not(is_hop_by_hop(header_name)) and header_name not in ("CONTENT-LENGTH", "CONTENT-TYPE"):
                header_value = ",".join(request.headers.getall(header_name))
                environ["HTTP_" + header_name.replace("-", "_")] = header_value
        # All done!
        return environ

    async def handle_request(self, request):
        # Check for body size overflow.
        if request.content_length is not None and request.content_length > self._max_request_body_size:
            raise HTTPRequestEntityTooLarge()
        # Buffer the body.
        async with ReadBuffer(self._inbuf_overflow, self._max_request_body_size, self._loop, self._executor) as body:
            while True:
                block = await request.content.readany()
                if not block:
                    break
                await body.write(block)
            # Seek the body.
            body, content_length = await body.get_body()
            # Get the environ.
            environ = self._get_environ(request, body, content_length)
            status, reason, headers, body = await self._loop.run_in_executor(
                self._executor,
                _run_application,
                self._application,
                environ,
            )
            # All done!
            return Response(
                status=status,
                reason=reason,
                headers=headers,
                body=body,
            )

    async def __call__(self, request):
        return await self.handle_request(request)


DEFAULTS = WSGIHandler.__init__.__kwdefaults__.copy()

HELP = {
    "application": "A WSGI application callable.",
    "url_scheme": (
        "A hint about the URL scheme used to access the application. Corresponds to ``environ['wsgi.url_scheme']``. "
        "Default is auto-detected to ``'http'`` or ``'https'``."
    ),
    "stderr": (
        "A file-like value for WSGI error logging. Corresponds to ``environ['wsgi.errors']``. "
        "Defaults to ``sys.stderr``."
    ),
    "inbuf_overflow": (
        "A tempfile will be created if the request body is larger than this value, which is measured in bytes. "
        "Defaults to ``{inbuf_overflow!r}``."
    ).format(**DEFAULTS),
    "max_request_body_size": (
        "Maximum number of bytes in request body. Defaults to ``{max_request_body_size!r}``. "
        "Larger requests will receive a HTTP 413 (Request Entity Too Large) response."
    ).format(**DEFAULTS),
    "executor": "An Executor instance used to run WSGI requests. Defaults to the :mod:`asyncio` base executor.",
    "loop": "The asyncio loop. Defaults to :func:`asyncio.get_event_loop`.",
}

WSGIHandler.__doc__ = WSGIHandler.__doc__.format(**HELP)
