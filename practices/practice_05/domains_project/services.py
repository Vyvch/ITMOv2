import os
import gzip
import time
import httpx
import logging
import datetime
from typing import List

import re2

from models import DomainItem, WaybackHistory
# История в веб-архиве: общая точка входа для API и CLI (логика — в wayback.py)
from wayback import (  # noqa: F401
    HistoryPending,
    WaybackError,
    get_service,
    reset_service,
    shutdown_service,
)

logger = logging.getLogger(__name__)

URLS = [
    "https://auction.nic.ru/downloads/ru_expiring_list.gz",
    "https://auction.nic.ru/downloads/su_expiring_list.gz",
    "https://auction.nic.ru/downloads/rf_expiring_list.gz",
]

_CACHE = {"data": [], "timestamp": 0}

def get_domains() -> List[DomainItem]:
    """
    Возвращает список всех освобождающихся доменов с датами.
    Использует кэширование (1 час), чтобы не спамить регистратора.
    """
    global _CACHE
    if _CACHE["data"] and time.time() - _CACHE["timestamp"] < 3600:
        return _CACHE["data"]
        
    freeing_domains: List[DomainItem] = []
    
    with httpx.Client(timeout=30.0) as client:
        for url in URLS:
            try:
                logger.info(f"Скачивание {url} ...")
                response = client.get(url)
                response.raise_for_status()
                
                content = gzip.decompress(response.content).decode("cp1251", errors="ignore")
                lines = content.splitlines()
                
                for line in lines:
                    parts = line.split("\t")
                    if len(parts) >= 3:
                        domain = parts[0].strip().lower()
                        if domain.startswith("xn--"):
                            try:
                                domain = domain.encode("ascii").decode("idna")
                            except (UnicodeError, ValueError):
                                pass
                        free_date = parts[2].strip()
                        if free_date != "Free-date":
                            freeing_domains.append(DomainItem(domain=domain, date=free_date))
            except httpx.RequestError as e:
                logger.error(f"Сетевая ошибка при скачивании {url}: {e}")
            except httpx.HTTPStatusError as e:
                logger.error(f"Ошибка статуса HTTP для {url}: {e}")
            except gzip.BadGzipFile as e:
                logger.error(f"Ошибка распаковки GZIP {url}: {e}")
            except Exception as e:
                logger.error(f"Непредвиденная ошибка при обработке {url}: {e}")
                
    _CACHE["data"] = freeing_domains
    _CACHE["timestamp"] = time.time()
    return freeing_domains

MAX_PATTERN_LENGTH = 256

class InvalidPatternError(ValueError):
    """Регулярное выражение невалидно, не поддерживается RE2 или слишком длинное."""

def filter_domains(domains: List[DomainItem], pattern: str) -> List[DomainItem]:
    """
    Оставляет домены, в имени которых есть совпадение с выражением (в любой позиции).
    Синтаксис RE2: линейное время, без обратных ссылок и lookaround. Регистр не учитывается.
    """
    if len(pattern) > MAX_PATTERN_LENGTH:
        raise InvalidPatternError(
            f"выражение длиннее {MAX_PATTERN_LENGTH} символов ({len(pattern)})"
        )

    options = re2.Options()
    options.case_sensitive = False
    options.log_errors = False
    try:
        compiled = re2.compile(pattern, options)
    except re2.error as e:
        reason = e.args[0] if e.args else ""
        if isinstance(reason, bytes):
            reason = reason.decode("utf-8", errors="replace")
        raise InvalidPatternError(reason or "некорректное выражение") from e

    return [d for d in domains if compiled.search(d.domain)]

def evaluate_top_domains(domains: List[DomainItem]) -> List[DomainItem]:
    """
    Отбирает 20 самых крутых доменов с помощью LLM.
    Предварительно фильтрует мусор (цифры, дефисы).
    """
    clean_domains = []
    for d in domains:
        name = d.domain.split(".")[0]
        if "-" not in name and not any(char.isdigit() for char in name):
            clean_domains.append(d)
            
    if len(clean_domains) < 20:
        clean_domains = domains
        
    candidates = sorted(clean_domains, key=lambda x: len(x.domain))[:1000]
    
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        logger.warning("DEEPSEEK_API_KEY не установлен.")
        result = []
        for c in candidates[:20]:
            c.reason = "Заглушка (нет ключа)"
            result.append(c)
        return result
        
    try:
        import openai
        import json
        client = openai.OpenAI(
            base_url="https://api.deepseek.com", 
            api_key=api_key
        )
        
        domains_text = "\n".join([f"{c.domain} (освобождается {c.date})" for c in candidates])
        
        prompt = (
            "Ты — эксперт по доменным именам. Из предоставленного списка выбери 20 самых ценных, "
            "красивых и осмысленных доменов (без мусора). "
            "Верни ответ СТРОГО в формате JSON: объект с ключом 'top', содержащий массив объектов с ключами "
            "'domain' (имя домена), "
            "'date' (дата освобождения), "
            "'reason' (краткое объяснение на 1-2 предложения, почему этот домен ценный и для какого бизнеса подойдёт).\n\n"
            "Список доменов:\n" + domains_text
        )
        
        response = client.chat.completions.create(
            model="deepseek-chat",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=3000,
            response_format={"type": "json_object"}
        )
        
        result_text = response.choices[0].message.content or "{}"
        data = json.loads(result_text)
        
        top_items = data.get("top", [])[:20]
        result = []
        for item in top_items:
            result.append(DomainItem(
                domain=item.get("domain", ""),
                date=item.get("date", ""),
                reason=item.get("reason", "")
            ))
        return result
    except openai.APIError as e:
        logger.error(f"Ошибка API LLM: {e}")
        return _fallback_error(candidates, f"API Error: {e}")
    except json.JSONDecodeError as e:
        logger.error(f"Ошибка парсинга JSON от LLM: {e}")
        return _fallback_error(candidates, "JSON Parse Error")
    except Exception as e:
        logger.error(f"Неожиданная ошибка LLM: {e}")
        return _fallback_error(candidates, f"Error: {e}")

def _fallback_error(candidates: List[DomainItem], error_msg: str) -> List[DomainItem]:
    result = []
    for c in candidates[:20]:
        c.reason = error_msg
        result.append(c)
    return result


async def get_wayback_history(domain: str) -> WaybackHistory:
    """
    История главной страницы домена в веб-архиве.
    Бросает WaybackError (InvalidDomain, ArchiveOverloaded, ArchiveTimeout, ArchiveUnavailable)
    или HistoryPending, если результат не готов к сроку ответа.
    """
    return await get_service().get_history(domain)
