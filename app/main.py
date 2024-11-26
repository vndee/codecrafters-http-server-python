import re
import gzip
import asyncio
import logging
import argparse

from enum import Enum
from dataclasses import dataclass
from typing import Optional, Dict, Callable, Tuple, List

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class HTTPStatus(Enum):
    """Enum for common HTTP status codes and messages."""
    OK = ('200 OK', 200)
    CREATED = ('201 Created', 201)
    NOT_FOUND = ('404 Not Found', 404)
    SERVER_ERROR = ('500 Internal Server Error', 500)


@dataclass
class HTTPResponse:
    """Represents an HTTP response with status, headers, and body."""
    status: HTTPStatus
    headers: Dict[str, str]
    body: bytes = b''

    @property
    def status_line(self) -> str:
        return self.status.value[0]


@dataclass
class Route:
    """Represents a route with its pattern, handler, and parameters."""
    pattern: str
    handler: Callable[..., HTTPResponse]
    params: Tuple[str, ...] = ()
    _compiled_pattern: Optional[re.Pattern] = None

    def __post_init__(self):
        """Compile the regex pattern once during initialization."""
        regex_pattern = self.pattern
        for param in self.params:
            regex_pattern = regex_pattern.replace(f"{{{param}}}", f"(?P<{param}>[^/]+)")
        self._compiled_pattern = re.compile(f"^{regex_pattern}$")

    def match(self, path: str) -> Tuple[bool, Dict[str, str]]:
        """Match the path against the pre-compiled route pattern."""
        if not self._compiled_pattern:
            return False, {}

        match = self._compiled_pattern.match(path)
        return (True, match.groupdict()) if match else (False, {})


@dataclass
class Request:
    """Represents an HTTP request with all its components."""
    method: str
    target: str
    headers: Dict[str, str]
    peer_info: Optional[Tuple[str, int]]
    body: bytes

    @property
    def accept_encoding(self) -> str:
        """Get the Accept-Encoding header value."""
        return self.headers.get('accept-encoding', '')


class CompressionHandler:
    """Handles compression-related functionality."""
    SUPPORTED_ENCODINGS = ['gzip']

    @classmethod
    def should_compress(cls, accept_encoding: str) -> str | None:
        """Determine if compression should be applied based on Accept-Encoding header."""
        if not accept_encoding:
            return None

        requested_encoding = [x.strip().lower() for x in accept_encoding.split(',')]

        for encoding in cls.SUPPORTED_ENCODINGS:
            if encoding in requested_encoding:
                return encoding

        return None

    @classmethod
    def add_compression_headers(cls, headers: Dict[str, str], accept_encoding: str) -> Dict[str, str]:
        """Add compression-related headers if compression should be applied."""
        encoding = cls.should_compress(accept_encoding)
        if encoding:
            headers['Content-Encoding'] = encoding

        return headers


class AsyncHTTPServer:
    """Asynchronous HTTP server with compression support."""

    def __init__(self, host: str = "localhost", port: int = 4221, directory: str = ".") -> None:
        self.host = host
        self.port = port
        self.directory = directory
        self.server: Optional[asyncio.Server] = None
        self.routes: Dict[str, List[Route]] = {}
        self.compression = CompressionHandler()

    def route(self, method: str, path: str, params: Tuple[str, ...] = ()) -> Callable:
        """Decorator to register routes with the server."""

        def decorator(handler: Callable[..., HTTPResponse]) -> Callable[..., HTTPResponse]:
            if method not in self.routes:
                self.routes[method] = []
            self.routes[method].append(Route(path, handler, params))
            return handler

        return decorator

    def create_response(self, response: HTTPResponse) -> bytes:
        """Convert HTTPResponse object to bytes."""
        response_lines = [f'HTTP/1.1 {response.status_line}']

        if response.body:
            response.headers['Content-Length'] = str(len(response.body))
        else:
            response.headers['Content-Length'] = '0'

        for key, value in response.headers.items():
            response_lines.append(f'{key}: {value}')

        response_lines.append('')
        http_response = '\r\n'.join(response_lines).encode('utf-8') + b'\r\n'

        if response.body:
            http_response += response.body

        return http_response

    async def parse_request(self, reader: asyncio.StreamReader) -> Optional[Request]:
        """Parse incoming HTTP request."""
        try:
            data = await reader.read(1024)
            if not data:
                return None

            request_parts = data.split(b'\r\n')
            request_line = request_parts[0].decode('utf-8')
            method, path, _ = request_line.split(' ')

            headers = {}
            current_line = 1
            while current_line < len(request_parts):
                line = request_parts[current_line].decode('utf-8').strip()
                if not line:
                    break
                if ':' in line:
                    key, value = line.split(':', 1)
                    headers[key.strip().lower()] = value.strip()
                current_line += 1

            body = b''
            if b'\r\n\r\n' in data:
                body = data.split(b'\r\n\r\n', 1)[1]

            return Request(
                method=method,
                target=path,
                headers=headers,
                body=body,
                peer_info=None  # Will be set in handle_client
            )
        except Exception as e:
            logger.error(f"Error parsing request: {e}")
            return None

    async def handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Handle individual client connections."""
        peer_info = writer.get_extra_info('peername')
        try:
            request = await self.parse_request(reader)
            if not request:
                return

            request.peer_info = peer_info
            logger.info(f"Received request from {peer_info}: {request.target}")

            response = await self.route_request(request)
            writer.write(self.create_response(response))
            await writer.drain()

        except Exception as e:
            logger.error(f"Error handling client {peer_info}: {e}")
        finally:
            try:
                writer.close()
                await writer.wait_closed()
                logger.info(f"Connection closed for {peer_info}")
            except Exception as e:
                logger.error(f"Error closing connection for {peer_info}: {e}")

    async def route_request(self, request: Request) -> HTTPResponse:
        """Route the request to appropriate handler."""
        routes = self.routes.get(request.method, [])

        for route in routes:
            matched, params = route.match(request.target)
            if matched:
                response = route.handler(request=request, **params)

                response.headers = self.compression.add_compression_headers(
                    response.headers,
                    request.accept_encoding
                )
                if response.headers.get('Content-Encoding') == 'gzip':
                    response.body = gzip.compress(response.body)

                return response

        return HTTPResponse(HTTPStatus.NOT_FOUND, {})

    async def start_server(self) -> None:
        """Start the HTTP server."""
        try:
            self.server = await asyncio.start_server(
                self.handle_client,
                self.host,
                self.port,
                reuse_port=True
            )

            logger.info(f"Server listening on {self.host}:{self.port}")
            async with self.server:
                await self.server.serve_forever()

        except Exception as e:
            logger.error(f"Server error: {e}")
        finally:
            if self.server:
                self.server.close()
                await self.server.wait_closed()
                logger.info("Server stopped")


def create_app(**kwargs) -> AsyncHTTPServer:
    """Create and configure the server application."""
    app = AsyncHTTPServer(**kwargs)

    @app.route('GET', '/', ())
    def handle_root(request: Request) -> HTTPResponse:
        return HTTPResponse(HTTPStatus.OK, {})

    @app.route('GET', '/echo/{message}', ('message',))
    def handle_echo(message: str, request: Request) -> HTTPResponse:
        body = message.encode('utf-8')
        headers = {
            'Content-Type': 'text/plain',
        }
        return HTTPResponse(HTTPStatus.OK, headers, body)

    @app.route('GET', '/user-agent', ())
    def handle_user_agent(request: Request) -> HTTPResponse:
        body = request.headers.get('user-agent', '').encode('utf-8')
        headers = {
            'Content-Type': 'text/plain',
        }
        return HTTPResponse(HTTPStatus.OK, headers, body)

    @app.route('GET', '/files/{filename}', ('filename',))
    def handle_file(filename: str, request: Request) -> HTTPResponse:
        try:
            with open(f"{app.directory}/{filename}", 'rb') as f:
                body = f.read()
                headers = {
                    'Content-Type': 'application/octet-stream',
                }
                return HTTPResponse(HTTPStatus.OK, headers, body)
        except FileNotFoundError:
            return HTTPResponse(HTTPStatus.NOT_FOUND, {})

    @app.route('POST', '/files/{filename}', ('filename',))
    def handle_post_file(filename: str, request: Request) -> HTTPResponse:
        try:
            with open(f"{app.directory}/{filename}", 'wb') as f:
                f.write(request.body)
                return HTTPResponse(HTTPStatus.CREATED, {})
        except Exception:
            return HTTPResponse(HTTPStatus.SERVER_ERROR, {})

    return app


async def shutdown(server: AsyncHTTPServer, signal_: asyncio.Event) -> None:
    """Handle graceful shutdown."""
    await signal_.wait()
    logger.info("Shutting down server...")
    if server.server:
        server.server.close()
        await server.server.wait_closed()


async def main(**kwargs) -> None:
    stop_event = asyncio.Event()
    server = create_app(**kwargs)

    server_task = asyncio.create_task(server.start_server())
    shutdown_task = asyncio.create_task(shutdown(server, stop_event))

    try:
        await asyncio.gather(server_task, shutdown_task)
    except KeyboardInterrupt:
        logger.info("Received shutdown signal")
        stop_event.set()
        await asyncio.gather(server_task, shutdown_task, return_exceptions=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="localhost", help="Host to listen on")
    parser.add_argument("--port", type=int, default=4221, help="Port to listen on")
    parser.add_argument("--directory", default=".", help="Directory to serve")
    args = parser.parse_args()

    asyncio.run(main(**vars(args)))