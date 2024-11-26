import asyncio
import logging
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
        match = re.match(regex_pattern, path)

        if match:
            return True, match.groupdict()
        return False, {}


class AsyncHTTPServer:
    def __init__(self, host: str = "localhost", port: int = 4221):
        self.host = host
        self.port = port
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
        response = '\r\n'.join(response_lines).encode('utf-8')

        if body:
            response = response + b'\r\n' + body

        return response

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

                logger.info(f"Received request from {peer_info}: {request_line}")

                # Find matching route
                routes = self.routes.get(method)
                print(routes)
                response = b''
                if routes:
                    matched = False
                    for route in routes:
                        matched, params = route.match(path)
                        if matched:
                            response = route.handler(**params)
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


def create_app() -> AsyncHTTPServer:
    """Create and configure the server application."""
    app = AsyncHTTPServer()

    @app.route('GET', '/', ())
    def handle_root() -> bytes:
        return app.create_response('200 OK')

    @app.route('GET', '/echo/{message}', ('message',))
    def handle_echo(message: str) -> bytes:
        body = message.encode('utf-8')
        headers = {
            'Content-Type': 'text/plain',
            'Content-Length': str(len(body))
        }
        return app.create_response('200 OK', headers, body)

    return app


async def main() -> None:
    stop_event = asyncio.Event()
    server = create_app()

    server_task = asyncio.create_task(server.start_server())
    shutdown_task = asyncio.create_task(shutdown(server, stop_event))

    try:
        await asyncio.gather(server_task, shutdown_task)
    except KeyboardInterrupt:
        logger.info("Received shutdown signal")
        stop_event.set()
        await asyncio.gather(server_task, shutdown_task, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())