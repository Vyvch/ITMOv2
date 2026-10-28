import sys
import subprocess
import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)

class MCPWhoisClient:
    """
    A persistent client for the local MCP WHOIS server (mcp_whois.py).
    Spawns the process once and sends initialization, then handles queries.
    """
    def __init__(self):
        self.process: Optional[subprocess.Popen] = None
        self._req_id = 1

    def start(self):
        """Start the MCP server subprocess and initialize it."""
        try:
            self.process = subprocess.Popen(
                [sys.executable, "mcp_whois.py"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1  # line buffered
            )
            
            # Send initialize
            req = {
                "jsonrpc": "2.0",
                "id": self._req_id,
                "method": "initialize",
                "params": {}
            }
            self._send_request(req)
            self._read_response() # Read initialize response
            self._req_id += 1
            logger.info("MCP WHOIS Client initialized successfully.")
        except Exception as e:
            logger.error(f"Failed to start MCP WHOIS Client: {e}")
            self.process = None

    def _send_request(self, req: dict):
        if not self.process or not self.process.stdin:
            raise RuntimeError("MCP process not running.")
        self.process.stdin.write(json.dumps(req) + "\n")
        self.process.stdin.flush()

    def _read_response(self) -> dict:
        if not self.process or not self.process.stdout:
            raise RuntimeError("MCP process not running.")
        line = self.process.stdout.readline()
        if not line:
            raise RuntimeError("MCP process stream ended.")
        return json.loads(line)

    def get_whois(self, domain: str) -> str:
        """Fetch WHOIS for a domain using the persistent MCP server."""
        if not self.process:
            self.start()
            
        if not self.process:
            return json.dumps({"error": "MCP Client could not be started"}, ensure_ascii=False)
            
        req = {
            "jsonrpc": "2.0",
            "id": self._req_id,
            "method": "tools/call",
            "params": {
                "name": "get_whois_info",
                "arguments": {"domain": domain}
            }
        }
        self._req_id += 1
        
        try:
            self._send_request(req)
            resp = self._read_response()
            
            if resp.get("result", {}).get("content"):
                return resp["result"]["content"][0]["text"]
            if resp.get("error"):
                return json.dumps({"error": resp["error"].get("message")}, ensure_ascii=False)
                
            return '{"error": "Нет данных"}'
        except Exception as e:
            logger.error(f"MCP client communication error: {e}")
            return json.dumps({"error": f"Ошибка связи с MCP: {e}"}, ensure_ascii=False)

    def close(self):
        """Terminate the MCP server subprocess."""
        if self.process:
            self.process.terminate()
            self.process = None

# Global instance for use across the application
mcp_client = MCPWhoisClient()
