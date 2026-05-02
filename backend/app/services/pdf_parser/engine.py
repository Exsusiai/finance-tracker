"""PDF parser engine — detects bank and delegates to bank-specific parser."""

from __future__ import annotations

import io
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import PdfImport


async def parse_pdf_statement(
    db: AsyncSession,
    pdf_import: PdfImport,
    content: bytes,
) -> dict[str, Any]:
    """Parse a PDF bank statement.
    
    Returns dict with:
        detected_bank: str | None
        parser_version: str
        statement_period: str | None
        raw_text: str
        error: str | None
        transactions: list[dict]
    """
    try:
        import pdfplumber
    except ImportError:
        return {
            "detected_bank": None,
            "parser_version": "0.1.0",
            "statement_period": None,
            "raw_text": "",
            "error": "pdfplumber not installed",
            "transactions": [],
        }

    # Extract text
    raw_text = ""
    try:
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text()
                if page_text:
                    raw_text += page_text + "\n"
    except Exception as e:
        return {
            "detected_bank": None,
            "parser_version": "0.1.0",
            "statement_period": None,
            "raw_text": "",
            "error": f"Failed to extract text: {e}",
            "transactions": [],
        }

    # Detect bank from text features
    detected_bank = _detect_bank(raw_text)

    # Parse transactions based on detected bank
    transactions = []
    if detected_bank:
        transactions = _parse_for_bank(detected_bank, raw_text)
    else:
        # Generic heuristic parser
        transactions = _parse_generic(raw_text)

    return {
        "detected_bank": detected_bank,
        "parser_version": "0.1.0",
        "statement_period": _detect_period(raw_text),
        "raw_text": raw_text[:10000],  # Store first 10k chars
        "error": None,
        "transactions": transactions,
    }


def _detect_bank(text: str) -> str | None:
    """Detect bank from text features."""
    text_lower = text.lower()
    bank_markers = {
        "icbc": ["工商银行", "中国工商银行", "icbc"],
        "cmb": ["招商银行", "china merchants bank", "cmb"],
        "ccb": ["建设银行", "中国建设银行", "ccb"],
        "boc": ["中国银行", "bank of china", "boc"],
        "n26": ["n26", "n26 bank"],
        "revolut": ["revolut"],
    }
    for bank, markers in bank_markers.items():
        for marker in markers:
            if marker.lower() in text_lower:
                return bank
    return None


def _detect_period(text: str) -> str | None:
    """Try to extract statement period from text."""
    import re
    # Match patterns like "2026年04月" or "2026-04" or "April 2026"
    patterns = [
        r"(\d{4})年(\d{2})月",
        r"(\d{4})-(\d{2})",
    ]
    for pattern in patterns:
        m = re.search(pattern, text)
        if m:
            return f"{m.group(1)}-{m.group(2)}"
    return None


def _parse_for_bank(bank: str, text: str) -> list[dict]:
    """Bank-specific parsing with real patterns."""
    bank_parsers = {
        "icbc": _parse_icbc,
        "cmb": _parse_cmb,
        "ccb": _parse_ccb,
        "boc": _parse_boc,
        "n26": _parse_n26,
        "revolut": _parse_revolut,
    }
    
    parser_func = bank_parsers.get(bank)
    if parser_func:
        return parser_func(text)
    else:
        return _parse_generic(text)


def _parse_generic(text: str) -> list[dict]:
    """Generic heuristic parser — extracts transaction-like lines."""
    import re
    transactions = []
    
    # Split text into lines and process each line
    for line in text.split('\n'):
        line = line.strip()
        if not line:
            continue
            
        # Match date first (supports YYYY-MM-DD and YYYY/MM/DD formats)
        date_match = re.search(r'(\d{4}[/-]\d{2}[/-]\d{2})', line)
        if not date_match:
            continue
            
        date = date_match.group(1)
        after_date = line[date_match.end():].strip()
        
        # Try to find amount (supports + and - prefixes)
        amount_match = re.search(r'([+-]?\d+\.?\d*)', after_date)
        if not amount_match:
            continue
            
        amount_str = amount_match.group(1)
        description = after_date[amount_match.end():].strip()
        
        # Skip if description is empty
        if not description:
            continue
            
        try:
            # Clean and convert amount
            amount_clean = amount_str.replace(',', '').strip()
            if not amount_clean:
                continue
                
            amount = float(amount_clean)
            if amount == 0:
                continue
            
            # Determine transaction type
            if amount > 0:
                tx_type = "income"
            else:
                tx_type = "expense"
                amount = abs(amount)
            
            transactions.append({
                "occurred_at": f"{date.replace('/', '-')}T00:00:00Z",
                "amount": amount,
                "currency": "CNY",  # Default to CNY
                "type": tx_type,
                "description": description,
                "raw_description": description,
                "counterparty": None,
                "external_id": f"pdf_gen_{len(transactions) + 1}",
            })
        except (ValueError, IndexError):
            continue
    
    return transactions


def _parse_icbc(text: str) -> list[dict]:
    """ICBC (工商银行) statement parser."""
    import re
    transactions = []
    
    # ICBC specific patterns
    patterns = [
        # ICBC format: 2026/05/01  收入  1,000.00  工资收入
        r'(\d{4}/\d{2}/\d{2})\s*[收]\s*([+-]?\d+\.?\d*)\s*([^,\n]+)',
        r'(\d{4}/\d{2}/\d{2})\s*[支]\s*([+-]?\d+\.?\d*)\s*([^,\n]+)',
        # ICBC format without +/- but with amounts
        r'(\d{4}/\d{2}/\d{2})\s*[收]\s*(\d+\.?\d*)\s*([^,\n]+)',
        r'(\d{4}/\d{2}/\d{2})\s*[支]\s*(\d+\.?\d*)\s*([^,\n]+)',
    ]
    
    for pattern in patterns:
        matches = re.findall(pattern, text)
        for match in matches:
            if len(match) >= 3:
                date, amount_str, description = match[0], match[1], match[2]
                
                try:
                    amount = float(amount_str.replace(',', ''))
                    if amount == 0:
                        continue
                    
                    # ICBC uses 收 (income) and 支 (expense)
                    if "收" in text[match.start():match.end()]:
                        tx_type = "income"
                    else:
                        tx_type = "expense"
                        amount = abs(amount)
                    
                    transactions.append({
                        "occurred_at": f"{date.replace('/', '-')}T00:00:00Z",
                        "amount": amount,
                        "currency": "CNY",
                        "type": tx_type,
                        "description": description.strip(),
                        "raw_description": description.strip(),
                        "counterparty": "ICBC",
                        "external_id": f"icbc_{len(transactions) + 1}",
                    })
                except (ValueError, IndexError):
                    continue
    
    return transactions


def _parse_cmb(text: str) -> list[dict]:
    """CMB (招商银行) statement parser."""
    import re
    transactions = []
    
    # CMB specific patterns
    patterns = [
        # CMB format: 2026-05-01  存入  +1000.00  工资
        r'(\d{4}-\d{2}-\d{2})\s*[存取]\s*([+-]?\d+\.?\d*)\s*([^,\n]+)',
        r'(\d{4}-\d{2}-\d{2})\s*([收付])\s*([+-]?\d+\.?\d*)\s*([^,\n]+)',
    ]
    
    for pattern in patterns:
        matches = re.findall(pattern, text)
        for match in matches:
            if len(match) >= 3:
                date, action, amount_str, description = match[0], match[1], match[2], match[3] if len(match) > 3 else ""
                
                try:
                    amount = float(amount_str.replace(',', ''))
                    if amount == 0:
                        continue
                    
                    # CMB uses 存 (deposit/income) and 取/付 (withdrawal/expense)
                    if action in ["存", "收"]:
                        tx_type = "income"
                    else:
                        tx_type = "expense"
                        amount = abs(amount)
                    
                    transactions.append({
                        "occurred_at": f"{date}T00:00:00Z",
                        "amount": amount,
                        "currency": "CNY",
                        "type": tx_type,
                        "description": description.strip(),
                        "raw_description": description.strip(),
                        "counterparty": "CMB",
                        "external_id": f"cmb_{len(transactions) + 1}",
                    })
                except (ValueError, IndexError):
                    continue
    
    return transactions


def _parse_ccb(text: str) -> list[dict]:
    """CCB (建设银行) statement parser."""
    import re
    transactions = []
    
    # CCB specific patterns
    patterns = [
        # CCB format: 20260501  收入  1000.00  工资
        r'(\d{8})\s*[收支]\s*(\d+\.?\d*)\s*([^,\n]+)',
    ]
    
    for pattern in patterns:
        matches = re.findall(pattern, text)
        for match in matches:
            if len(match) >= 3:
                date_str, amount_str, description = match[0], match[1], match[2]
                
                try:
                    # Convert date from YYYYMMDD to YYYY-MM-DD
                    date = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"
                    amount = float(amount_str.replace(',', ''))
                    if amount == 0:
                        continue
                    
                    # CCB basic classification
                    if "收" in text:
                        tx_type = "income"
                    else:
                        tx_type = "expense"
                        amount = abs(amount)
                    
                    transactions.append({
                        "occurred_at": f"{date}T00:00:00Z",
                        "amount": amount,
                        "currency": "CNY",
                        "type": tx_type,
                        "description": description.strip(),
                        "raw_description": description.strip(),
                        "counterparty": "CCB",
                        "external_id": f"ccb_{len(transactions) + 1}",
                    })
                except (ValueError, IndexError):
                    continue
    
    return transactions


def _parse_boc(text: str) -> list[dict]:
    """BOC (中国银行) statement parser."""
    import re
    transactions = []
    
    # BOC specific patterns
    patterns = [
        # BOC format: 2026-05-01  收入  1000.00  工资收入
        r'(\d{4}-\d{2}-\d{2})\s*[收入]\s*(\d+\.?\d*)\s*([^,\n]+)',
        r'(\d{4}-\d{2}-\d{2})\s*[支出]\s*(\d+\.?\d*)\s*([^,\n]+)',
    ]
    
    for pattern in patterns:
        matches = re.findall(pattern, text)
        for match in matches:
            if len(match) >= 3:
                date, amount_str, description = match[0], match[1], match[2]
                
                try:
                    amount = float(amount_str.replace(',', ''))
                    if amount == 0:
                        continue
                    
                    # BOC uses 收入 (income) and 支出 (expense)
                    if "收" in text[match.start():match.end()]:
                        tx_type = "income"
                    else:
                        tx_type = "expense"
                        amount = abs(amount)
                    
                    transactions.append({
                        "occurred_at": f"{date}T00:00:00Z",
                        "amount": amount,
                        "currency": "CNY",
                        "type": tx_type,
                        "description": description.strip(),
                        "raw_description": description.strip(),
                        "counterparty": "BOC",
                        "external_id": f"boc_{len(transactions) + 1}",
                    })
                except (ValueError, IndexError):
                    continue
    
    return transactions


def _parse_n26(text: str) -> list[dict]:
    """N26 bank statement parser."""
    import re
    transactions = []
    
    # N26 specific patterns
    patterns = [
        # N26 format: 2026-05-01  SPENDING  -25.00  Merchant Name
        r'(\d{4}-\d{2}-\d{2})\s*(DEPOSIT|SPENDING|TRANSFER|PENDING)\s*([+-]?\d+\.?\d*)\s*([^,\n]+)',
        # Alternative format: 2026/05/01  -25.00  Merchant Name
        r'(\d{4}/\d{2}/\d{2})\s*([+-]?\d+\.?\d*)\s*([^,\n]+)',
    ]
    
    for pattern in patterns:
        matches = re.findall(pattern, text)
        for match in matches:
            if len(match) >= 3:
                date, action, amount_str, description = match[0], match[1], match[2], match[3] if len(match) > 3 else ""
                
                try:
                    amount = float(amount_str.replace(',', ''))
                    if amount == 0:
                        continue
                    
                    # N26 classification
                    if action == "DEPOSIT" or (action == "" and amount > 0):
                        tx_type = "income"
                    else:
                        tx_type = "expense"
                        amount = abs(amount)
                    
                    transactions.append({
                        "occurred_at": f"{date.replace('/', '-')}T00:00:00Z",
                        "amount": amount,
                        "currency": "EUR",  # N26 typically uses EUR
                        "type": tx_type,
                        "description": description.strip(),
                        "raw_description": description.strip(),
                        "counterparty": "N26",
                        "external_id": f"n26_{len(transactions) + 1}",
                    })
                except (ValueError, IndexError):
                    continue
    
    return transactions


def _parse_revolut(text: str) -> list[dict]:
    """Revolut bank statement parser."""
    import re
    transactions = []
    
    # Revolut specific patterns
    patterns = [
        # Revolut format: 2026-05-01  Card Payment  -25.00  Merchant Name
        r'(\d{4}-\d{2}-\d{2})\s*(Card Payment|Top-Up|Exchange|Transfer| ATM Withdrawal)\s*([+-]?\d+\.?\d*)\s*([^,\n]+)',
        # Alternative: 2026-05-01  -25.00  Merchant Name
        r'(\d{4}-\d{2}-\d{2})\s*([+-]?\d+\.?\d*)\s*([^,\n]+)',
    ]
    
    for pattern in patterns:
        matches = re.findall(pattern, text)
        for match in matches:
            if len(match) >= 3:
                date, action, amount_str, description = match[0], match[1], match[2], match[3] if len(match) > 3 else ""
                
                try:
                    amount = float(amount_str.replace(',', ''))
                    if amount == 0:
                        continue
                    
                    # Revolut classification
                    if action in ["Top-Up", "Transfer IN"] or (action == "" and amount > 0):
                        tx_type = "income"
                    else:
                        tx_type = "expense"
                        amount = abs(amount)
                    
                    transactions.append({
                        "occurred_at": f"{date}T00:00:00Z",
                        "amount": amount,
                        "currency": "EUR",  # Revolut typically uses EUR
                        "type": tx_type,
                        "description": description.strip(),
                        "raw_description": description.strip(),
                        "counterparty": "Revolut",
                        "external_id": f"revolut_{len(transactions) + 1}",
                    })
                except (ValueError, IndexError):
                    continue
    
    return transactions
