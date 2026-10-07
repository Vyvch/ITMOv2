import argparse
import datetime
import logging
from services import get_domains, evaluate_top_domains
from mcp_client import mcp_client

logger = logging.getLogger(__name__)

def run_cli():
    parser = argparse.ArgumentParser(description="Утилита для просмотра освобождающихся доменов.")
    parser.add_argument("command", choices=["list", "top"], help="Команда для выполнения: list (все) или top (лучшие)")
    
    args = parser.parse_args()

    domains_data = get_domains()

    if not domains_data:
        print("Список доменов пуст.")
        return

    if args.command == "list":
        for d in domains_data:
            print(f"{d.date} - {d.domain}")
    elif args.command == "top":
        mcp_client.start()
        try:
            today_str = datetime.date.today().isoformat()
            target_domains = [d for d in domains_data if d.date == today_str]
            
            print(f"Оцениваем крутость доменов (LLM) из {len(target_domains)} кандидатов на сегодня...")
            top_20 = evaluate_top_domains(target_domains)
            
            print("\nТоп доменов и их WHOIS:\n" + "="*40)
            for item in top_20:
                print(f"Домен: {item.domain} ({item.date})")
                print(f"Почему крутой: {item.reason}")
                whois_info = mcp_client.get_whois(item.domain)
                short_whois = whois_info[:200].replace('\n', ' ') + "..." if len(whois_info) > 200 else whois_info
                print(f"WHOIS: {short_whois}\n" + "-"*40)
        finally:
            mcp_client.close()

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    run_cli()
