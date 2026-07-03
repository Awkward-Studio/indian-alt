import json
import logging
import re
import requests
from typing import Any

from bs4 import BeautifulSoup
from django.db import transaction
from django.utils import timezone

from ai_orchestrator.services.embedding_processor import EmbeddingService
from ai_orchestrator.services.llm_providers import AnthropicProviderService
from deals.models import (
    Deal,
    VentureIntelligenceCompanyProfile,
    VentureIntelligenceCompanyRelation,
    VentureIntelligenceFinancialStatement,
    VentureIntelligenceRelationType,
    VentureIntelligenceStatementType,
)
from deals.services.venture_intelligence import normalize_cin

logger = logging.getLogger(__name__)


def _extract_json_object(text: str) -> dict[str, Any]:
    cleaned = (text or "").strip()
    if not cleaned:
        return {}
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if not match:
            return {}
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            return {}


def _clean_text(value: Any, *, max_length: int = 500) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:max_length]


def _normalize_exchange(value: Any) -> str:
    text = _clean_text(value, max_length=40).upper()
    if text in {"NATIONAL STOCK EXCHANGE", "NSE INDIA"}:
        return "NSE"
    if text in {"BOMBAY STOCK EXCHANGE"}:
        return "BSE"
    return text


def _first_present(row: dict[str, Any], keys: list[str]) -> Any:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return value
    return None


def _number_from_value(value: Any) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value)
    negative = "(" in text and ")" in text
    cleaned = re.sub(r"[^0-9.\-]", "", text.replace(",", ""))
    if cleaned in {"", "-", ".", "-."}:
        return None
    try:
        parsed = float(cleaned)
        return -parsed if negative and parsed > 0 else parsed
    except ValueError:
        return None


def _format_fy_for_comps(date=None) -> str:
    current = date or timezone.localdate()
    if 4 <= current.month <= 9:
        fy_year = current.year
    elif current.month >= 10:
        fy_year = current.year + 1
    else:
        fy_year = current.year
    return f"FY{str(fy_year)[-2:]}"


def _fy_from_period(period: Any) -> str:
    text = _clean_text(period, max_length=40)
    match = re.search(r"\b(?:Mar|March)\s+(\d{4})\b", text, re.IGNORECASE)
    if match:
        return f"FY{match.group(1)[-2:]}"
    return text or "N/A"


def _metric_key(value: Any) -> str:
    text = _clean_text(value, max_length=120).lower()
    text = text.replace("+", "")
    text = text.replace("%", " percent")
    text = re.sub(r"[^a-z0-9]+", "_", text).strip("_")
    aliases = {
        "sales": "sales",
        "revenue": "sales",
        "operating_revenue": "sales",
        "expenses": "expenses",
        "operating_profit": "operating_profit",
        "opm_percent": "opm_percent",
        "other_income": "other_income",
        "interest": "interest",
        "depreciation": "depreciation",
        "profit_before_tax": "profit_before_tax",
        "tax_percent": "tax_percent",
        "net_profit": "net_profit",
        "eps_in_rs": "eps",
        "dividend_payout_percent": "dividend_payout_percent",
        "equity_capital": "equity_capital",
        "reserves": "reserves",
        "borrowings": "borrowings",
        "other_liabilities": "other_liabilities",
        "total_liabilities": "total_liabilities",
        "fixed_assets": "fixed_assets",
        "cwip": "cwip",
        "investments": "investments",
        "other_assets": "other_assets",
        "total_assets": "total_assets",
        "cash_from_operating_activity": "cash_from_operations",
        "cash_from_investing_activity": "cash_from_investing",
        "cash_from_financing_activity": "cash_from_financing",
        "net_cash_flow": "net_cash_flow",
        "free_cash_flow": "free_cash_flow",
        "cfo_op": "cfo_op",
        "debtor_days": "debtor_days",
        "inventory_days": "inventory_days",
        "days_payable": "days_payable",
        "cash_conversion_cycle": "cash_conversion_cycle",
        "working_capital_days": "working_capital_days",
        "roce_percent": "roce_percent",
    }
    return aliases.get(text, text)


def _extract_company_name(soup: BeautifulSoup, fallback: str = "") -> str:
    heading = soup.select_one("h1")
    if heading:
        text = _clean_text(heading.get_text(" ", strip=True), max_length=200)
        if text:
            return text
    title = soup.select_one("title")
    if title:
        text = _clean_text(title.get_text(" ", strip=True), max_length=200)
        text = re.sub(r"\s*-\s*Screener.*$", "", text, flags=re.IGNORECASE).strip()
        if text:
            return text
    return fallback


def _extract_ticker_from_url(url: str) -> str:
    match = re.search(r"/company/([^/]+)/?", url or "", re.IGNORECASE)
    if not match:
        return ""
    symbol = match.group(1).upper()
    return "" if symbol in {"", "COMPARE"} else symbol


def _company_match_key(value: Any) -> str:
    text = _clean_text(value, max_length=200).lower()
    text = text.replace("&", " and ")
    text = re.sub(
        r"\b(ltd|limited|india|pvt|private|plc|inc|corp|corporation|co|company)\b",
        " ",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _select_current_peer(peers: list[dict[str, Any]], *, company_name: str, ticker: str = "") -> dict[str, Any]:
    if not peers:
        return {}

    ticker_key = _clean_text(ticker, max_length=40).upper()
    if ticker_key:
        ticker_pattern = re.compile(rf"/company/{re.escape(ticker_key)}(?:/|$)", re.IGNORECASE)
        for peer in peers:
            if ticker_pattern.search(str(peer.get("url") or "")):
                return peer

    company_key = _company_match_key(company_name)
    if not company_key:
        return {}

    for peer in peers:
        if _company_match_key(peer.get("company")) == company_key:
            return peer

    for peer in peers:
        peer_key = _company_match_key(peer.get("company"))
        if len(company_key) >= 5 and len(peer_key) >= 5 and (company_key in peer_key or peer_key in company_key):
            return peer

    return {}


def _extract_warehouse_id(soup: BeautifulSoup) -> str:
    info = soup.select_one("#company-info[data-warehouse-id]")
    return _clean_text(info.get("data-warehouse-id") if info else "", max_length=40)


def _parse_peers_table_html(html: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html or "", "lxml")
    table = soup.select_one("table")
    if not table:
        return []
    trs = table.select("tr")
    if len(trs) < 2:
        return []
    headers = [_metric_key(cell.get_text(" ", strip=True)) for cell in trs[0].select("th, td")]
    peers: list[dict[str, Any]] = []
    for tr in trs[1:]:
        cells = tr.select("td, th")
        if len(cells) < 2:
            continue
        row: dict[str, Any] = {"source": "Screener peers"}
        for index, cell in enumerate(cells):
            key = headers[index] if index < len(headers) and headers[index] else f"column_{index}"
            text = _clean_text(cell.get_text(" ", strip=True), max_length=160)
            link = cell.select_one("a[href]")
            if key in {"name", "company", "company_name"} or index == 1:
                row["company"] = text
                if link:
                    row["url"] = link.get("href")
            elif key != "s_no":
                row[key] = _number_from_value(text)
        if row.get("company"):
            peers.append(row)
    return peers


def _parse_screener_section_table(soup: BeautifulSoup, section_id: str) -> list[dict[str, Any]]:
    section = soup.select_one(f"section#{section_id}")
    if not section:
        return []
    table = section.select_one("table")
    if not table:
        return []
    rows = []
    for tr in table.select("tr"):
        cells = [_clean_text(cell.get_text(" ", strip=True), max_length=120) for cell in tr.select("th, td")]
        if any(cells):
            rows.append(cells)
    if len(rows) < 2:
        return []
    headers = rows[0][1:]
    period_rows = [
        {"period": header, "fy": _fy_from_period(header), "source": "Screener"}
        for header in headers
        if header
    ]
    for row in rows[1:]:
        if len(row) < 2:
            continue
        key = _metric_key(row[0])
        if not key:
            continue
        for index, value in enumerate(row[1:len(period_rows) + 1]):
            period_rows[index][key] = _number_from_value(value)
    return period_rows


def _parse_screener_peers(soup: BeautifulSoup) -> list[dict[str, Any]]:
    section = soup.select_one("section#peers")
    if not section:
        return []
    table = section.select_one("table")
    if not table:
        return []
    header_cells = table.select("thead tr th")
    if not header_cells:
        first_row = table.select_one("tr")
        header_cells = first_row.select("th, td") if first_row else []
    headers = [_metric_key(cell.get_text(" ", strip=True)) for cell in header_cells]
    return _parse_peers_table_html(str(table))


def _period_sort_year(row: dict[str, Any]) -> int | None:
    text = _clean_text(row.get("fy") or row.get("period"), max_length=40)
    fy_match = re.search(r"\bFY(\d{2}|\d{4})\b", text, re.IGNORECASE)
    if fy_match:
        year = int(fy_match.group(1))
        return 2000 + year if year < 100 else year
    year_match = re.search(r"\b(20\d{2}|19\d{2})\b", text)
    if year_match:
        return int(year_match.group(1))
    return None


def _latest_period_row(rows: list[dict[str, Any]]) -> dict[str, Any]:
    dated = [row for row in rows if isinstance(row, dict) and row.get("period")]
    if not dated:
        return rows[-1] if rows else {}
    return max(dated, key=lambda row: (_period_sort_year(row) or -1, dated.index(row)))


def _latest_snapshot_year(snapshot: dict[str, Any]) -> int:
    years = [
        _period_sort_year(row)
        for row in snapshot.get("profit_loss") or []
        if isinstance(row, dict)
    ]
    return max([year for year in years if year is not None], default=0)


def _normalize_trading_comps(payload: dict[str, Any], *, company_name: str) -> dict[str, Any]:
    raw = payload.get("trading_comps") if isinstance(payload.get("trading_comps"), dict) else {}
    market_cap = _first_present(raw, ["equity_value", "market_cap"]) or payload.get("market_cap")
    net_debt = _first_present(raw, ["net_debt"])
    ev = _first_present(raw, ["ev", "enterprise_value", "ev_rs_cr"]) or payload.get("enterprise_value")
    sales = _first_present(raw, ["sales", "revenue", "operating_revenue"])
    ebitda = _first_present(raw, ["ebitda", "operating_profit"])
    ev_num = _number_from_value(ev)
    market_cap_num = _number_from_value(market_cap)
    net_debt_num = _number_from_value(net_debt)
    sales_num = _number_from_value(sales)
    ebitda_num = _number_from_value(ebitda)
    ev_sales = _number_from_value(_first_present(raw, ["ev_sales", "ev_to_sales", "revenue_multiple"]))
    ev_ebitda = _number_from_value(_first_present(raw, ["ev_ebitda", "ev_to_ebitda", "ebitda_multiple"]))
    latest_pl = _latest_period_row(payload.get("profit_loss") or [])
    if sales is None:
        sales = _first_present(latest_pl, ["sales", "revenue"])
        sales_num = _number_from_value(sales)
    if ebitda is None:
        ebitda = _first_present(latest_pl, ["operating_profit", "ebitda"])
        ebitda_num = _number_from_value(ebitda)
    if ev_num is None:
        ev_num = market_cap_num
    if ev_sales is None and ev_num is not None and sales_num:
        ev_sales = ev_num / sales_num
    if ev_ebitda is None and ev_num is not None and ebitda_num:
        ev_ebitda = ev_num / ebitda_num
    fy = raw.get("fy") or latest_pl.get("fy")
    return {
        "date": raw.get("date"),
        "company": raw.get("company") or company_name,
        "investor": raw.get("investor"),
        "fy": fy,
        "gross_margin": _number_from_value(raw.get("gross_margin")),
        "ebitda_margin": _number_from_value(raw.get("ebitda_margin")),
        "net_debt": net_debt_num,
        "equity_value": market_cap_num,
        "ev": ev_num,
        "sales": sales_num,
        "ev_sales": ev_sales,
        "ebitda": ebitda_num,
        "ev_ebitda": ev_ebitda,
        "source": raw.get("source") or "Screener",
        "raw": raw,
    }


def _apply_trading_comps_to_financial_row(row: dict[str, Any], trading_comps: dict[str, Any]) -> dict[str, Any]:
    data = {**row, "source": row.get("source") or "Screener"}
    if not trading_comps:
        return data

    ev = _number_from_value(trading_comps.get("ev"))
    equity_value = _number_from_value(trading_comps.get("equity_value"))
    if ev is None:
        ev = equity_value
    sales = _number_from_value(row.get("sales")) or _number_from_value(trading_comps.get("sales"))
    ebitda = _number_from_value(row.get("operating_profit")) or _number_from_value(row.get("ebitda")) or _number_from_value(trading_comps.get("ebitda"))

    data.update({
        "date": trading_comps.get("date"),
        "company": trading_comps.get("company"),
        "investor": trading_comps.get("investor"),
        "fy_comps": trading_comps.get("fy"),
        "gross_margin": trading_comps.get("gross_margin"),
        "ebitda_margin": trading_comps.get("ebitda_margin"),
        "net_debt": trading_comps.get("net_debt"),
        "equity_value": equity_value,
        "ev": ev,
        "sales": sales,
        "ev_sales": (ev / sales) if ev is not None and sales else _number_from_value(trading_comps.get("ev_sales")),
        "ebitda": ebitda,
        "ev_ebitda": (ev / ebitda) if ev is not None and ebitda else _number_from_value(trading_comps.get("ev_ebitda")),
    })
    return data


def _screener_url_for(ticker: str = "", screener_url: str = "") -> str:
    if screener_url:
        return screener_url
    symbol = _clean_text(ticker, max_length=40).upper()
    if not symbol:
        return ""
    return f"https://www.screener.in/company/{symbol}/consolidated/"


def _parse_screener_number(value: Any) -> float | None:
    parsed = _number_from_value(value)
    return parsed


def _extract_top_ratios(html: str) -> dict[str, Any]:
    soup = BeautifulSoup(html or "", "lxml")
    ratios: dict[str, Any] = {}
    for item in soup.select("#top-ratios li"):
        name = item.select_one(".name")
        number = item.select_one(".number")
        if not name or not number:
            continue
        key = re.sub(r"\s+", " ", name.get_text(" ", strip=True)).strip().lower()
        value_text = re.sub(r"\s+", " ", number.get_text(" ", strip=True)).strip()
        ratios[key] = {
            "display": value_text,
            "number": _parse_screener_number(value_text),
        }
    return ratios


def _normalize_profit_loss_row(row: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(row)
    normalized["revenue"] = _first_present(row, ["revenue", "sales", "operating_revenue", "operating_income", "total_income"])
    normalized["ebitda"] = _first_present(row, ["ebitda", "operating_profit", "operating_profit_after_other_income"])
    normalized["pat"] = _first_present(row, ["pat", "net_profit", "profit_after_tax", "net_profit_after_tax"])
    return normalized


def _normalize_balance_sheet_row(row: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(row)
    normalized["total_assets"] = _first_present(row, ["total_assets", "assets"])
    normalized["total_liabilities"] = _first_present(row, ["total_liabilities", "liabilities"])
    normalized["net_worth"] = _first_present(row, ["net_worth", "equity", "shareholders_funds", "reserves"])
    normalized["borrowings"] = _first_present(row, ["borrowings", "debt", "total_debt"])
    normalized["cash_and_equivalents"] = _first_present(row, ["cash_and_equivalents", "cash", "cash_equivalents"])
    return normalized


def _normalize_cash_flow_row(row: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(row)
    normalized["cash_from_operations"] = _first_present(row, ["cash_from_operations", "operating_cash_flow", "cfo"])
    normalized["cash_from_investing"] = _first_present(row, ["cash_from_investing", "investing_cash_flow", "cfi"])
    normalized["cash_from_financing"] = _first_present(row, ["cash_from_financing", "financing_cash_flow", "cff"])
    normalized["net_cash_flow"] = _first_present(row, ["net_cash_flow", "net_change_in_cash"])
    return normalized


class ScreenerCompanyService:
    """
    Fetches listed-company competitor data from Screener HTML and stores it in
    the existing company profile and relation tables.
    """

    def fetch_public_company_snapshot(self, company_name: str, *, ticker: str = "", exchange: str = "") -> dict[str, Any]:
        direct = self.fetch_screener_direct_snapshot(ticker=ticker)
        if direct:
            return direct

        resolved = self.resolve_screener_url(company_name, ticker=ticker, exchange=exchange)
        direct = self.fetch_screener_direct_snapshot(
            ticker=resolved.get("ticker") or ticker,
            screener_url=resolved.get("screener_url") or "",
        )
        if direct:
            for key in ("ticker", "exchange", "industry", "sector", "website"):
                if not direct.get(key) and resolved.get(key):
                    direct[key] = resolved.get(key)
            return direct

        return {
            "is_listed": False,
            "company_name": company_name,
            "ticker": ticker,
            "exchange": exchange,
            "summary": "No parseable Screener page was found.",
            "sources": [],
            "resolver_raw_response": resolved.get("raw_response"),
        }

    def resolve_screener_url(self, company_name: str, *, ticker: str = "", exchange: str = "") -> dict[str, Any]:
        prompt = (
            "Use web search only to find the official Screener.in page for this listed Indian company. "
            "Do not extract or estimate any financial values. If no Screener page is found, return "
            "{\"is_listed\": false}.\n\n"
            f"Company: {company_name}\n"
            f"Ticker hint: {ticker or 'N/A'}\n"
            f"Exchange hint: {exchange or 'N/A'}\n\n"
            "Return exactly one JSON object and no markdown:\n"
            "{\n"
            "  \"is_listed\": true,\n"
            "  \"company_name\": \"Company Ltd\",\n"
            "  \"registered_name\": \"Company Limited\",\n"
            "  \"ticker\": \"COMPANY\",\n"
            "  \"exchange\": \"NSE\",\n"
            "  \"screener_url\": \"https://www.screener.in/company/COMPANY/\",\n"
            "  \"website\": \"https://company.example\",\n"
            "  \"industry\": \"Industry\",\n"
            "  \"sector\": \"Sector\"\n"
            "}"
        )
        service = AnthropicProviderService()
        result = service.execute_standard(
            {
                "model": "default",
                "system": "You find official Screener company URLs. Return only valid JSON.",
                "prompt": prompt,
                "options": {
                    "max_tokens": 800,
                    "temperature": 0.0,
                    "max_search_uses": 3,
                    "web_search_tool_type": "web_search_20250305",
                },
            },
            timeout=60,
        )
        payload = _extract_json_object(result.get("response") or "")
        payload["raw_response"] = result.get("response") or ""
        return payload

    def fetch_screener_direct_snapshot(self, *, ticker: str = "", screener_url: str = "") -> dict[str, Any]:
        url = _screener_url_for(ticker=ticker, screener_url=screener_url)
        if not url:
            return {}
        candidate_urls = [url]
        symbol = _clean_text(ticker, max_length=40).upper()
        if symbol and not screener_url and url.rstrip("/").endswith("/consolidated"):
            candidate_urls.append(f"https://www.screener.in/company/{symbol}/")

        snapshots = []
        for candidate_url in candidate_urls:
            try:
                response = requests.get(
                    candidate_url,
                    headers={
                        "User-Agent": (
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/120.0.0.0 Safari/537.36"
                        )
                    },
                    timeout=20,
                )
                response.raise_for_status()
            except Exception as exc:
                logger.warning("Direct Screener fetch failed for %s: %s", candidate_url, exc)
                continue
            soup = BeautifulSoup(response.text or "", "lxml")
            endpoint_peers = self.fetch_screener_peers(
                warehouse_id=_extract_warehouse_id(soup),
                referer=response.url,
            )
            snapshots.append(self.parse_screener_html(
                response.text,
                url=response.url,
                fallback_ticker=ticker,
                endpoint_peers=endpoint_peers,
            ))

        if not snapshots:
            return {}
        return max(snapshots, key=lambda item: (_latest_snapshot_year(item), len(item.get("profit_loss") or [])))

    def fetch_screener_peers(self, *, warehouse_id: str = "", referer: str = "") -> list[dict[str, Any]]:
        if not warehouse_id:
            return []
        base_match = re.match(r"^(https?://[^/]+)", referer or "")
        base_url = base_match.group(1) if base_match else "https://www.screener.in"
        url = f"{base_url}/api/company/{warehouse_id}/peers/"
        try:
            response = requests.get(
                url,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    ),
                    "Referer": referer or base_url,
                },
                timeout=20,
            )
            response.raise_for_status()
        except Exception as exc:
            logger.warning("Direct Screener peers fetch failed for %s: %s", url, exc)
            return []
        return _parse_peers_table_html(response.text)

    def parse_screener_html(
        self,
        html: str,
        *,
        url: str,
        fallback_ticker: str = "",
        endpoint_peers: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        soup = BeautifulSoup(html or "", "lxml")
        ratios = _extract_top_ratios(html)
        peers = endpoint_peers or _parse_screener_peers(soup)
        company_name = _extract_company_name(soup, fallback_ticker)
        ticker_value = _clean_text(fallback_ticker or _extract_ticker_from_url(url), max_length=40).upper()
        current_peer = _select_current_peer(peers, company_name=company_name, ticker=ticker_value)
        market_cap = current_peer.get("mar_cap_rs_cr")
        if market_cap is None:
            market_cap = (ratios.get("market cap") or {}).get("number")
        current_price = current_peer.get("cmp_rs")
        if current_price is None:
            current_price = (ratios.get("current price") or {}).get("number")
        stock_pe = current_peer.get("p_e")
        if stock_pe is None:
            stock_pe = (ratios.get("stock p/e") or {}).get("number")
        book_value = (ratios.get("book value") or {}).get("number")
        roe = (ratios.get("roe") or {}).get("number")
        roce = current_peer.get("roce_percent")
        if roce is None:
            roce = (ratios.get("roce") or {}).get("number")
        profit_loss = _parse_screener_section_table(soup, "profit-loss")
        balance_sheet = _parse_screener_section_table(soup, "balance-sheet")
        cash_flow = _parse_screener_section_table(soup, "cash-flow")
        quarterly = _parse_screener_section_table(soup, "quarters")
        ratios_table = _parse_screener_section_table(soup, "ratios")
        latest_pl = _latest_period_row(profit_loss)
        market_cap_num = _number_from_value(market_cap)
        sales_num = _number_from_value(latest_pl.get("sales"))
        ebitda_num = _number_from_value(latest_pl.get("operating_profit"))
        trading_comps = {
            "date": None,
            "company": company_name,
            "investor": None,
            "fy": latest_pl.get("fy"),
            "equity_value": market_cap_num,
            "ev": market_cap_num,
            "sales": sales_num,
            "ev_sales": (market_cap_num / sales_num) if market_cap_num is not None and sales_num else None,
            "ebitda": ebitda_num,
            "ev_ebitda": (market_cap_num / ebitda_num) if market_cap_num is not None and ebitda_num else None,
            "source": "Screener direct",
        }
        return {
            "is_listed": bool(market_cap or profit_loss or peers),
            "company_name": company_name,
            "registered_name": company_name,
            "ticker": ticker_value,
            "exchange": "",
            "screener_url": url,
            "url": url,
            "market_cap": market_cap,
            "current_price": current_price,
            "stock_pe": stock_pe,
            "book_value": book_value,
            "roe": roe,
            "roce": roce,
            "trading_comps": trading_comps,
            "profit_loss": profit_loss,
            "balance_sheet": balance_sheet,
            "cash_flow": cash_flow,
            "quarterly_financials": quarterly,
            "ratios": ratios_table,
            "peers": peers,
            "summary": f"Screener public-market profile parsed directly for {company_name}." if company_name else "",
            "sources": [{"title": "Screener", "url": url}],
            "raw_top_ratios": ratios,
        }

    def normalize_snapshot(self, payload: dict[str, Any], *, fallback_name: str, fallback_ticker: str = "", fallback_exchange: str = "") -> dict[str, Any]:
        sources = payload.get("sources") if isinstance(payload.get("sources"), list) else []
        profit_loss = payload.get("profit_loss") if isinstance(payload.get("profit_loss"), list) else []
        if not profit_loss:
            profit_loss = payload.get("annual_financials") if isinstance(payload.get("annual_financials"), list) else []
        balance_sheet = payload.get("balance_sheet") if isinstance(payload.get("balance_sheet"), list) else []
        cash_flow = payload.get("cash_flow") if isinstance(payload.get("cash_flow"), list) else []
        quarterly = payload.get("quarterly_financials") if isinstance(payload.get("quarterly_financials"), list) else []
        return {
            "is_listed": bool(payload.get("is_listed", True)),
            "name": _clean_text(payload.get("company_name") or payload.get("name") or fallback_name, max_length=200),
            "registered_name": _clean_text(payload.get("registered_name") or payload.get("company_name") or fallback_name, max_length=250),
            "ticker": _clean_text(payload.get("ticker") or fallback_ticker, max_length=40).upper(),
            "exchange": _normalize_exchange(payload.get("exchange") or fallback_exchange),
            "screener_url": _clean_text(payload.get("screener_url"), max_length=500),
            "website": _clean_text(payload.get("website"), max_length=500),
            "industry": _clean_text(payload.get("industry"), max_length=200),
            "sector": _clean_text(payload.get("sector"), max_length=200),
            "market_cap": _clean_text(payload.get("market_cap"), max_length=200),
            "summary": _clean_text(payload.get("summary"), max_length=1500),
            "profit_loss": [_normalize_profit_loss_row(row) for row in profit_loss if isinstance(row, dict)],
            "balance_sheet": [_normalize_balance_sheet_row(row) for row in balance_sheet if isinstance(row, dict)],
            "cash_flow": [_normalize_cash_flow_row(row) for row in cash_flow if isinstance(row, dict)],
            "quarterly_financials": [row for row in quarterly if isinstance(row, dict)],
            "snapshot": {
                "current_price": payload.get("current_price"),
                "stock_pe": payload.get("stock_pe"),
                "book_value": payload.get("book_value"),
                "roe": payload.get("roe"),
                "roce": payload.get("roce"),
                "trading_comps": _normalize_trading_comps(payload, company_name=_clean_text(payload.get("company_name") or fallback_name, max_length=200)),
                "peers": payload.get("peers") if isinstance(payload.get("peers"), list) else [],
                "ratios": payload.get("ratios") if isinstance(payload.get("ratios"), list) else [],
                "top_ratios": payload.get("raw_top_ratios") if isinstance(payload.get("raw_top_ratios"), dict) else {},
                "sources": sources,
            },
            "raw": payload,
        }

    @transaction.atomic
    def save_public_competitor(self, deal: Deal, competitor: dict[str, Any]) -> VentureIntelligenceCompanyProfile:
        name = _clean_text(competitor.get("name") or competitor.get("company_name"), max_length=200)
        if not name:
            raise ValueError("Missing public competitor name.")

        snapshot = self.normalize_snapshot(
            self.fetch_public_company_snapshot(
                name,
                ticker=competitor.get("ticker") or "",
                exchange=competitor.get("exchange") or "",
            ),
            fallback_name=name,
            fallback_ticker=competitor.get("ticker") or "",
            fallback_exchange=competitor.get("exchange") or "",
        )
        if not snapshot["is_listed"]:
            raise ValueError("Screener/public-market research did not confirm this is a listed company.")

        cin = normalize_cin(competitor.get("cin"))
        lookup = {}
        if cin:
            lookup["cin"] = cin
        elif snapshot["ticker"]:
            existing = VentureIntelligenceCompanyProfile.objects.filter(
                ticker__iexact=snapshot["ticker"],
                exchange__iexact=snapshot["exchange"] or "",
            ).first()
            if existing:
                lookup["id"] = existing.id
        if not lookup:
            existing = VentureIntelligenceCompanyProfile.objects.filter(name__iexact=snapshot["name"]).first()
            if existing:
                lookup["id"] = existing.id
        defaults = {
            "name": snapshot["name"],
            "registered_name": snapshot["registered_name"],
            "website": snapshot["website"] or None,
            "industry": snapshot["industry"] or None,
            "sector": snapshot["sector"] or None,
            "listing_status": "Listed",
            "data_source": "screener",
            "company_type": "listed_public",
            "exchange": snapshot["exchange"] or None,
            "ticker": snapshot["ticker"] or None,
            "screener_url": snapshot["screener_url"] or None,
            "market_cap": snapshot["market_cap"] or None,
            "public_market_snapshot": snapshot["snapshot"],
            "business_description": snapshot["summary"] or None,
            "raw_profile_json": snapshot["raw"],
        }
        if lookup:
            profile, _created = VentureIntelligenceCompanyProfile.objects.update_or_create(
                **lookup,
                defaults=defaults,
            )
        else:
            profile = VentureIntelligenceCompanyProfile.objects.create(
                cin=cin or None,
                **defaults,
            )

        VentureIntelligenceFinancialStatement.objects.filter(
            company_profile=profile,
            statement_type__in=[
                VentureIntelligenceStatementType.PROFIT_LOSS,
                VentureIntelligenceStatementType.BALANCE_SHEET,
                VentureIntelligenceStatementType.CASH_FLOW,
                VentureIntelligenceStatementType.SCREENER_ANNUAL,
                VentureIntelligenceStatementType.SCREENER_QUARTERLY,
            ],
        ).delete()
        trading_comps = (snapshot.get("snapshot") or {}).get("trading_comps") or {}
        for row in snapshot["profit_loss"]:
            data = _apply_trading_comps_to_financial_row(row, trading_comps)
            VentureIntelligenceFinancialStatement.objects.create(
                company_profile=profile,
                statement_type=VentureIntelligenceStatementType.PROFIT_LOSS,
                fy=str(row.get("fy") or row.get("year") or "N/A")[:20],
                fin_type="Consolidated",
                data=data,
            )
        for row in snapshot["balance_sheet"]:
            VentureIntelligenceFinancialStatement.objects.create(
                company_profile=profile,
                statement_type=VentureIntelligenceStatementType.BALANCE_SHEET,
                fy=str(row.get("fy") or row.get("year") or "N/A")[:20],
                fin_type="Consolidated",
                data={**row, "source": row.get("source") or "Screener"},
            )
        for row in snapshot["cash_flow"]:
            VentureIntelligenceFinancialStatement.objects.create(
                company_profile=profile,
                statement_type=VentureIntelligenceStatementType.CASH_FLOW,
                fy=str(row.get("fy") or row.get("year") or "N/A")[:20],
                fin_type="Consolidated",
                data={**row, "source": row.get("source") or "Screener"},
            )
        for row in snapshot["quarterly_financials"]:
            VentureIntelligenceFinancialStatement.objects.create(
                company_profile=profile,
                statement_type=VentureIntelligenceStatementType.SCREENER_QUARTERLY,
                fy=str(row.get("fy") or row.get("quarter") or "N/A")[:20],
                fin_type="Screener",
                data=row,
            )

        VentureIntelligenceCompanyRelation.objects.update_or_create(
            deal=deal,
            company_profile=profile,
            defaults={
                "relation_type": VentureIntelligenceRelationType.COMPETITOR,
                "notes": competitor.get("notes") or "",
            },
        )
        return profile

    def index_profile_for_rag(self, profile: VentureIntelligenceCompanyProfile, *, deal: Deal | None = None) -> str:
        lines = [
            f"# Public Company Dossier: {profile.name}",
            f"- **Data Source**: Screener/public-market web research",
        ]
        if profile.ticker or profile.exchange:
            lines.append(f"- **Ticker / Exchange**: {profile.ticker or 'N/A'} / {profile.exchange or 'N/A'}")
        if profile.screener_url:
            lines.append(f"- **Screener URL**: {profile.screener_url}")
        if profile.market_cap:
            lines.append(f"- **Market Cap**: {profile.market_cap}")
        if profile.industry or profile.sector:
            lines.append(f"- **Industry/Sector**: {profile.industry or 'N/A'} / {profile.sector or 'N/A'}")
        if profile.business_description:
            lines.append(f"\n## Summary\n{profile.business_description}")
        snapshot = profile.public_market_snapshot or {}
        if snapshot:
            lines.append("\n## Public Market Snapshot")
            for key, value in snapshot.items():
                if key == "sources" or value in (None, "", []):
                    continue
                lines.append(f"- **{key.replace('_', ' ').title()}**: {value}")
            trading_comps = snapshot.get("trading_comps") if isinstance(snapshot, dict) else {}
            if isinstance(trading_comps, dict) and trading_comps:
                lines.append("\n## Trading Comps Row")
                for key in ("date", "company", "investor", "fy", "ev", "sales", "ev_sales", "ebitda", "ev_ebitda"):
                    value = trading_comps.get(key)
                    if value not in (None, "", []):
                        lines.append(f"- **{key.replace('_', ' ').title()}**: {value}")
        financials = profile.financial_statements.filter(
            statement_type__in=[
                VentureIntelligenceStatementType.PROFIT_LOSS,
                VentureIntelligenceStatementType.BALANCE_SHEET,
                VentureIntelligenceStatementType.CASH_FLOW,
                VentureIntelligenceStatementType.SCREENER_ANNUAL,
                VentureIntelligenceStatementType.SCREENER_QUARTERLY,
            ]
        )
        if financials.exists():
            lines.append("\n## Screener Financials")
            for fs in financials:
                lines.append(f"\n### {fs.get_statement_type_display()} ({fs.fy})")
                for key, value in fs.data.items():
                    if key in {"fy", "year", "quarter"}:
                        continue
                    lines.append(f"- **{key.replace('_', ' ').title()}**: {value}")
        sources = snapshot.get("sources") if isinstance(snapshot, dict) else []
        if isinstance(sources, list) and sources:
            lines.append("\n## Sources")
            for source in sources[:6]:
                if isinstance(source, dict):
                    lines.append(f"- {source.get('title') or 'Source'}: {source.get('url') or 'N/A'}")

        dossier = "\n".join(lines)
        EmbeddingService().chunk_and_embed(
            text=dossier,
            deal=deal,
            source_type='extracted_source',
            source_id=f"screener_{profile.id}",
            metadata={
                "company_name": profile.name,
                "ticker": profile.ticker,
                "exchange": profile.exchange,
                "data_source": "screener",
            },
            replace_existing=True,
        )
        return dossier
