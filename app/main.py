import asyncio
import logging
from typing import Optional

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class AsyncHTTPServer:
    def __init__(self, host: str = "localhost", port: int = 4221):
        self.host = host
        self.port = port
        self.server: Optional[asyncio.Server] = None

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
                # request = data.decode('utf-8')
                logger.info(f"Received request from {peer_info}: {request}")

                response = (
                    "HTTP/1.1 200 OK\r\n"
                    "Content-Type: text/plain\r\n"
                    "Connection: keep-alive\r\n"
                    "\r\n"
                    "Hello, World!"
                )

                # Send response
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

            # Keep the server running
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


async def main() -> None:
    stop_event = asyncio.Event()
    server = AsyncHTTPServer()

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
