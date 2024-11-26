import asyncio
import logging
import argparse
from typing import Optional, Dict, Callable, Tuple, List
from dataclasses import dataclass
import re

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


@dataclass
class Route:
    pattern: str
    handler: Callable[..., bytes]
    params: Tuple[str, ...] = ()

    def match(self, path: str) -> Tuple[bool, Dict[str, str]]:
        """Match the path against the route pattern and extract parameters."""
        regex_pattern = self.pattern
        for param in self.params:
            regex_pattern = regex_pattern.replace(f"{{{param}}}", f"(?P<{param}>[^/]+)")

        regex_pattern = f"^{regex_pattern}$"
        # print(f"Matching {path} against {regex_pattern}")
        match = re.match(regex_pattern, path)

        if match:
            return True, match.groupdict()
        return False, {}


@dataclass
class Request:
    method: str
    target: str
    headers: Dict[str, str]
    peer_info: Tuple[str, int]
    body: bytes


class AsyncHTTPServer:
    def __init__(self, host: str = "localhost", port: int = 4221, directory: str = ".") -> None:
        self.host = host
        self.port = port
        self.directory = directory
        self.server: Optional[asyncio.Server] = None
        self.routes: Dict[str, List[Route]] = {}

    def route(self, method: str, path: str, params: Tuple[str, ...] = ()) -> Callable:
        """Decorator to register routes with the server."""

        def decorator(handler: Callable[..., bytes]) -> Callable[..., bytes]:
            if method not in self.routes:
                self.routes[method] = []

            self.routes[method].append(Route(path, handler, params))
            return handler

        return decorator

    def create_response(self, status: str, headers: Dict[str, str] = None, body: bytes = b'') -> bytes:
        """Create HTTP response with headers and body."""
        response_lines = [f'HTTP/1.1 {status}']

        if headers:
            for key, value in headers.items():
                response_lines.append(f'{key}: {value}')

        response_lines.append('')  # Empty line between headers and body
        response = '\r\n'.join(response_lines).encode('utf-8') + b'\r\n'

        if body:
            response = response + body

        return response

    def parse_headers(self, request: List[bytes]) -> Dict[str, str]:
        """Parse HTTP headers from request."""
        headers = {}
        for line in request[1:]:  # Skip the request line
            decoded_line = line.decode('utf-8').strip()
            if not decoded_line:  # Empty line marks the end of headers
                break

            if ':' in decoded_line:
                key, value = decoded_line.split(':', 1)
                headers[key.strip().lower()] = value.strip()

        return headers

    async def handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Handle individual client connections."""
        peer_info = writer.get_extra_info('peername')
        try:
            logger.info(f"New connection from {peer_info}")

            while True:
                data = await reader.read(1024)
                if not data:
                    break

                request = data.split(b'\r\n')
                request_line = request[0].decode('utf-8')
                method, path, _ = request_line.split(' ')
                headers = self.parse_headers(request)

                logger.info(f"Received request from {peer_info}: {request}")

                routes = self.routes.get(method)
                payload = Request(
                    method=method,
                    target=path,
                    headers=headers,
                    body=b'',
                    peer_info=peer_info
                )

                response = b''
                if routes:
                    matched = False
                    for route in routes:
                        matched, params = route.match(path)
                        if matched:
                            response = route.handler(request=payload, **params)
                            break

                    if not matched:
                        response = self.create_response('404 Not Found')
                else:
                    response = self.create_response('404 Not Found')

                writer.write(response)
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

    async def start_server(self) -> None:
        """Start the HTTP server."""
        try:
            self.server = await asyncio.start_server(
                self.handle_client,
                self.host,
                self.port,
                reuse_port=True,
                start_serving=True
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


async def shutdown(server: AsyncHTTPServer, signal_: asyncio.Event) -> None:
    """Handle graceful shutdown."""
    await signal_.wait()
    logger.info("Shutting down server...")
    if server.server:
        server.server.close()
        await server.server.wait_closed()


def create_app(**kwargs) -> AsyncHTTPServer:
    """Create and configure the server application."""
    app = AsyncHTTPServer(**kwargs)

    @app.route('GET', '/', ())
    def handle_root(request: Request) -> bytes:
        return app.create_response('200 OK')

    @app.route('GET', '/echo/{message}', ('message',))
    def handle_echo(message: str, request: Request) -> bytes:
        body = message.encode('utf-8')
        headers = {
            'Content-Type': 'text/plain',
            'Content-Length': str(len(body))
        }
        return app.create_response('200 OK', headers, body)

    @app.route('GET', '/user-agent', ())
    def handle_user_agent(request: Request) -> bytes:
        user_agent = request.headers.get('user-agent', '')
        body = user_agent.encode('utf-8')
        headers = {
            'Content-Type': 'text/plain',
            'Content-Length': str(len(body))
        }
        return app.create_response('200 OK', headers, body)

    @app.route('GET', '/files/{filename}', ('filename',))
    def handle_file(filename: str, request: Request) -> bytes:
        try:
            with open(f"{app.directory}/{filename}", 'rb') as f:
                body = f.read()
                headers = {
                    'Content-Type': 'application/octet-stream',
                    'Content-Length': str(len(body))
                }
                return app.create_response('200 OK', headers, body)
        except FileNotFoundError:
            return app.create_response('404 Not Found')

    @app.route('POST', '/files/{filename}', ('filename',))
    def handle_post_file(filename: str, request: Request) -> bytes:
        try:
            with open(f"{app.directory}/{filename}", 'wb') as f:
                f.write(request.body)
                return app.create_response('201 Created')
        except Exception:
            return app.create_response('500 Internal Server Error')

    return app


async def main(
    **kwargs
) -> None:
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
    args_parser = argparse.ArgumentParser()
    args_parser.add_argument("--host", default="localhost", help="Host to listen on")
    args_parser.add_argument("--port", type=int, default=4221, help="Port to listen on")
    args_parser.add_argument("--directory", default=".", help="Directory to serve")
    args = args_parser.parse_args()

    asyncio.run(main(**vars(args)))