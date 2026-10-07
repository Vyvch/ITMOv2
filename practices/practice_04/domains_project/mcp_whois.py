import sys
import json
import socket
import logging

logger = logging.getLogger(__name__)

def get_ru_whois(domain: str) -> dict:
    """
    Fetch WHOIS information for a given .ru, .su, or .rf domain.

    Args:
        domain (str): The domain name to query.

    Returns:
        dict: A dictionary containing the WHOIS parsed data or error message.
    """
    domain_idna = domain.encode('idna').decode('ascii')
    
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(5.0)
    try:
        s.connect(("whois.tcinet.ru", 43))
        s.send((domain_idna + "\r\n").encode("ascii"))
        response = b""
        while True:
            data = s.recv(4096)
            if not data: break
            response += data
    except (socket.timeout, socket.error) as e:
        return {"error": f"Network error during WHOIS query: {e}"}
    finally:
        s.close()
    
    text = response.decode('utf-8', errors='replace')
    
    if "No entries found" in text:
        return {"status": "Свободен (нет записей в WHOIS)"}
        
    res = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("%"): continue
        if ":" in line:
            parts = line.split(":", 1)
            key = parts[0].strip().lower()
            val = parts[1].strip()
            
            if key == "state": res["status"] = val
            elif key == "registrar": res["registrar"] = val
            elif key == "created": res["creation_date"] = val
            elif key == "paid-till": res["expiration_date"] = val
            elif key == "free-date": res["free_date"] = val
            
    if not res:
        return {"error": "Не удалось распарсить WHOIS", "raw": text[:500]}
        
    return res

def handle_request(req: dict) -> dict:
    """
    Process an incoming JSON-RPC request according to the MCP protocol.

    Args:
        req (dict): The parsed JSON-RPC request object.

    Returns:
        dict: The JSON-RPC response object.
    """
    method = req.get("method")
    req_id = req.get("id")
    
    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {
                    "tools": {}
                },
                "serverInfo": {
                    "name": "whois-mcp",
                    "version": "1.0.0"
                }
            }
        }
    elif method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "tools": [
                    {
                        "name": "get_whois_info",
                        "description": "Fetch WHOIS information for a given domain.",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "domain": {"type": "string", "description": "Domain name to lookup"}
                            },
                            "required": ["domain"]
                        }
                    }
                ]
            }
        }
    elif method == "tools/call":
        domain = req.get("params", {}).get("arguments", {}).get("domain", "")
        try:
            res_dict = get_ru_whois(domain)
            info_text = json.dumps(res_dict, ensure_ascii=False)
        except (ValueError, TypeError) as e:
            info_text = json.dumps({"error": f"Data error: {str(e)}"}, ensure_ascii=False)
            
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "content": [
                    {
                        "type": "text",
                        "text": info_text
                    }
                ],
                "isError": False
            }
        }
        
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": "Method not found"}}

def main():
    """
    Main entry point for the MCP server.
    Reads JSON-RPC requests from standard input and writes responses to standard output.
    """
    while True:
        line = sys.stdin.readline()
        if not line:
            break
        try:
            req = json.loads(line)
            resp = handle_request(req)
            if resp:
                print(json.dumps(resp), flush=True)
        except json.JSONDecodeError:
            # Ignore malformed JSON
            pass

if __name__ == "__main__":
    main()
