"""Dual-path customs declaration extraction using SiliconFlow's OpenAI API."""

import base64
import copy
import json
import logging
import math
import re
from io import BytesIO
from dataclasses import dataclass, field
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple

import httpx
import pymupdf as fitz
import pdfplumber
from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI
from PIL import Image

from app.core.config import settings
from app.schemas.customs_schema import CustomsDeclarationData

logger = logging.getLogger(__name__)

# All tunables come from the shared Settings instance; this module must not load
# .env itself, so callers and tests cannot end up with a different model or
# timeout than the rest of the application.
SILICONFLOW_BASE_URL = settings.SILICONFLOW_BASE_URL
TEXT_EXTRACT_MODEL = settings.TEXT_EXTRACT_MODEL
DEFAULT_VISION_MODEL = "Qwen/Qwen3-VL-8B-Instruct"
DEPRECATED_VISION_MODELS = {"Qwen/Qwen2.5-VL-72B-Instruct"}


def _build_vision_model_candidates(configured_model: Optional[str] = None) -> list[str]:
    configured = (configured_model if configured_model is not None else settings.VISION_EXTRACT_MODEL).strip()
    fallback = settings.VISION_EXTRACT_FALLBACK_MODEL.strip()
    if configured in DEPRECATED_VISION_MODELS:
        configured = DEFAULT_VISION_MODEL
    if fallback in DEPRECATED_VISION_MODELS:
        fallback = DEFAULT_VISION_MODEL
    return list(dict.fromkeys(model for model in (configured or DEFAULT_VISION_MODEL, fallback or DEFAULT_VISION_MODEL) if model not in DEPRECATED_VISION_MODELS))


VISION_MODEL_CANDIDATES = _build_vision_model_candidates()
VISION_EXTRACT_MODEL = VISION_MODEL_CANDIDATES[0]
VISION_MAX_IMAGE_SIDE = settings.VISION_MAX_IMAGE_SIDE
READ_TIMEOUT = settings.CUSTOMS_LLM_READ_TIMEOUT
DEFAULT_LLM_TIMEOUT = httpx.Timeout(connect=15.0, read=READ_TIMEOUT, write=30.0, pool=15.0)
BYPASS_PROXY = settings.LLM_BYPASS_SYSTEM_PROXY

_DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y年%m月%d日")
_DATE_SEPARATOR_PATTERN = re.compile(r"^(\d{4})\s*[./年-]\s*(\d{1,2})\s*[./月-]\s*(\d{1,2})\s*日?$")
_NUMBER_PATTERN = re.compile(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)")

MOCK_DECLARATION = CustomsDeclarationData(
    pre_entry_no="202651849302194857",
    domestic_consignor="深圳市博润优品贸易有限公司 (4403169821)",
    overseas_consignee="BORUN KITCHEN & BATH LLC (USA)",
    export_port="深圳湾海关 (5345)",
    export_date="2026-10-26",
    declaration_date="2026-10-27",
    supervision_mode="跨境电商B2B直接出口 (9710)",
    transport_mode="水路运输 (2)",
    package_count=70,
    package_type="纸箱",
    gross_weight=1180.0,
    net_weight=1250.0,
    fob_total_amount=15660.0,
    items=[
        {"item_no": 1, "hs_code": "8481804090", "product_name": "304不锈钢厨房抽拉水龙头 (拉丝镍)", "quantity": 500, "unit": "套", "unit_price": 18.50, "total_price": 9250.0, "currency": "USD", "origin_country": "中国 (CHN)", "destination_country": "美国 (USA)", "duty_mode": "照章征税"},
        {"item_no": 2, "hs_code": "7324100000", "product_name": "纳米黑金刚厨房手工水槽 (双槽)", "quantity": 200, "unit": "个", "unit_price": 31.75, "total_price": 6410.0, "currency": "USD", "origin_country": "中国 (CHN)", "destination_country": "美国 (USA)", "duty_mode": "照章征税"},
    ],
)

MOCK_PASSED_DECLARATION = MOCK_DECLARATION.model_copy(deep=True)
MOCK_PASSED_DECLARATION.gross_weight = 1250.0
MOCK_PASSED_DECLARATION.net_weight = 1180.0
MOCK_PASSED_DECLARATION.fob_total_amount = 15600.0
MOCK_PASSED_DECLARATION.items[1].total_price = 6350.0

# Demo vision payloads stay deterministic: the anomaly sample deliberately swaps
# gross/net weight so the demo still exercises MODEL_CONFLICT.
MOCK_ANOMALY_VISION: Dict[str, Any] = {
    "pre_entry_no": MOCK_DECLARATION.pre_entry_no,
    "gross_weight": 1250.0,
    "net_weight": 1180.0,
    "package_count": 70,
    "fob_total_amount": 15600.0,
}
MOCK_PASSED_VISION: Dict[str, Any] = {
    "pre_entry_no": MOCK_PASSED_DECLARATION.pre_entry_no,
    "gross_weight": MOCK_PASSED_DECLARATION.gross_weight,
    "net_weight": MOCK_PASSED_DECLARATION.net_weight,
    "package_count": MOCK_PASSED_DECLARATION.package_count,
    "fob_total_amount": MOCK_PASSED_DECLARATION.fob_total_amount,
}


@dataclass
class CustomsExtractionResult:
    """Extraction output carrying provenance so callers never mistake samples for model data."""

    data: CustomsDeclarationData
    vision_data: Dict[str, Any]
    extraction_source: str  # "real" | "mock" | "fallback"
    vision_status: str  # "ok" | "failed" | "skipped"
    degraded_reasons: List[str] = field(default_factory=list)


class CustomsExtractionError(RuntimeError):
    """Real-mode extraction failure that must surface to the API caller instead of a sample."""

    def __init__(self, status_code: int, message: str, reasons: List[str]):
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.reasons = list(reasons)


def pdf_first_page_to_base64(pdf_path: str) -> str:
    """Render the first PDF page at 144 DPI and return a resized JPEG data URL."""
    with fitz.open(pdf_path) as document:
        if document.page_count == 0:
            raise ValueError("PDF 文件没有页面")
        page = document.load_page(0)
        pixmap = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
        image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
        image.thumbnail((VISION_MAX_IMAGE_SIDE, VISION_MAX_IMAGE_SIDE), Image.Resampling.LANCZOS)
        buffer = BytesIO()
        image.save(buffer, format="JPEG", quality=85, optimize=True)
        encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
        return f"data:image/jpeg;base64,{encoded}"


def _api_key() -> str:
    return (
        settings.SILICONFLOW_API_KEY
        or settings.OPENAI_API_KEY
        or settings.API_KEY
        or ""
    )


def _client() -> OpenAI:
    key = _api_key()
    if not key:
        raise RuntimeError("未配置 SILICONFLOW_API_KEY / OPENAI_API_KEY / API_KEY")
    http_client = httpx.Client(timeout=DEFAULT_LLM_TIMEOUT, trust_env=not BYPASS_PROXY)
    return OpenAI(
        api_key=key,
        base_url=SILICONFLOW_BASE_URL,
        timeout=DEFAULT_LLM_TIMEOUT,
        max_retries=2,
        http_client=http_client,
    )


def _parse_json_content(content: Optional[str]) -> Dict[str, Any]:
    if not content:
        raise ValueError("模型未返回 JSON 内容")
    parsed = json.loads(content)
    if not isinstance(parsed, dict):
        raise ValueError("模型响应 JSON 顶层必须是对象")
    return parsed


def _safe_float(value: Any) -> float:
    """Extract a finite number from model output without raising on malformed values."""
    if value is None or isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        try:
            number = float(value)
            return number if math.isfinite(number) else 0.0
        except (TypeError, ValueError, OverflowError):
            return 0.0
    text = str(value).replace(",", "").strip()
    match = _NUMBER_PATTERN.search(text)
    if not match:
        return 0.0
    try:
        number = float(match.group(0))
        return number if math.isfinite(number) else 0.0
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _normalize_date_str(val: Any) -> str:
    """Normalize common customs date formats to YYYY-MM-DD, preserving unknown text."""
    if val is None or isinstance(val, bool):
        return ""
    text = str(val).strip()
    if not text or text.lower() in {"0", "null", "none"}:
        return ""
    if re.fullmatch(r"\d{8}", text):
        candidate = f"{text[:4]}-{text[4:6]}-{text[6:8]}"
        try:
            return datetime.strptime(candidate, "%Y-%m-%d").strftime("%Y-%m-%d")
        except ValueError:
            return text
    match = _DATE_SEPARATOR_PATTERN.fullmatch(text)
    if match:
        candidate = f"{int(match.group(1)):04d}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"
        try:
            return datetime.strptime(candidate, "%Y-%m-%d").strftime("%Y-%m-%d")
        except ValueError:
            return text
    for date_format in _DATE_FORMATS:
        try:
            return datetime.strptime(text, date_format).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return text


def _normalize_and_clean_customs_data(data: dict) -> dict:
    """Normalize aliases/dates, discard printed blank goods rows, and reconcile header total."""
    if not isinstance(data, dict):
        return {}
    normalized = copy.deepcopy(data)

    def empty_value(value: Any) -> bool:
        if value is None or isinstance(value, bool):
            return True
        if isinstance(value, (int, float)) and value == 0:
            return True
        return str(value).strip().lower() in ("", "0", "0.0", "null", "none")

    def fill_from_alias(target: str, aliases: Tuple[str, ...]) -> None:
        current = normalized.get(target)
        if not empty_value(current):
            return
        for alias in aliases:
            value = normalized.get(alias)
            if not empty_value(value):
                normalized[target] = value
                return

    # Map import/export document wording onto the stable frontend contract.
    fill_from_alias("export_date", ("import_date", "进口日期", "出口日期"))
    is_import_declaration = any(key in normalized for key in ("import_date", "进口日期", "进境关别", "境内收货人", "境外发货人"))
    if is_import_declaration:
        for target, aliases in (
            ("export_port", ("import_customs", "entry_customs", "进境关别")),
            ("domestic_consignor", ("domestic_consignee", "国内收货人", "境内收货人")),
            ("overseas_consignee", ("overseas_consignor", "overseas_shipper", "境外发货人")),
        ):
            for alias in aliases:
                value = normalized.get(alias)
                if not empty_value(value):
                    normalized[target] = value
                    break
    else:
        fill_from_alias("export_port", ("customs_office", "export_customs", "出境关别"))
        fill_from_alias("domestic_consignor", ("domestic_consignee", "国内收货人", "境内收货人", "境内发货人"))
        fill_from_alias("overseas_consignee", ("overseas_consignor", "overseas_shipper", "境外发货人", "境外收货人"))
    normalized["export_date"] = _normalize_date_str(normalized.get("export_date"))
    normalized["declaration_date"] = _normalize_date_str(normalized.get("declaration_date"))

    item_keys = ("items", "commodities", "commodity_items")
    source_key = next((key for key in item_keys if isinstance(normalized.get(key), list) and normalized[key]), None)
    if source_key is None:
        source_key = next((key for key in item_keys if isinstance(normalized.get(key), list)), "items")
    source_items = normalized.get(source_key, [])
    if not isinstance(source_items, list):
        source_items = []
    clean_items = []

    def has_text(value: Any) -> bool:
        if value is None or isinstance(value, bool):
            return False
        return str(value).strip().lower() not in ("", "0", "null", "none")

    for raw_item in source_items:
        if not isinstance(raw_item, dict):
            continue
        item = copy.deepcopy(raw_item)
        hs_code = item.get("hs_code") or item.get("commodity_code") or item.get("code", "")
        product_name = item.get("product_name") or item.get("name") or item.get("description", "")
        quantity = _safe_float(item.get("quantity", item.get("qty", 0)))
        total_price = _safe_float(item.get("total_price"))
        if total_price <= 0:
            total_price = _safe_float(item.get("total_amount", 0))
        if not (has_text(hs_code) or has_text(product_name) or quantity > 0 or total_price > 0):
            continue

        # Keep any model-provided alias fields while adding canonical fields required by the API schema.
        item_no = len(clean_items) + 1
        if not str(item.get("hs_code") or "").strip():
            item["hs_code"] = hs_code or ""
        if not str(item.get("product_name") or "").strip():
            item["product_name"] = product_name or ""
        item["quantity"] = quantity
        item.setdefault("unit", "")
        unit_price = _safe_float(item.get("unit_price"))
        item["unit_price"] = unit_price if unit_price > 0 else _safe_float(item.get("price", 0))
        item["total_price"] = total_price
        item.setdefault("currency", "")
        item.setdefault("origin_country", "")
        item.setdefault("destination_country", "")
        item.setdefault("duty_mode", "")
        item["item_no"] = item_no
        if "index" in item:
            item["index"] = item_no
        clean_items.append(item)

    normalized[source_key] = clean_items
    if source_key != "items":
        normalized["items"] = clean_items

    items_total = round(sum(_safe_float(item.get("total_price")) for item in clean_items), 2)
    total_keys = ("fob_total_amount", "fob_total_price", "total_amount", "declared_total_price")
    existing_total_keys = [key for key in total_keys if key in normalized]
    header_total = next((_safe_float(normalized[key]) for key in existing_total_keys if _safe_float(normalized[key]) > 0), 0.0)
    canonical_total = _safe_float(normalized.get("fob_total_amount"))
    if canonical_total <= 0 and header_total > 0:
        normalized["fob_total_amount"] = header_total
    if header_total <= 0 and items_total > 0:
        for key in existing_total_keys:
            normalized[key] = items_total
        normalized["fob_total_amount"] = items_total
    elif "fob_total_amount" not in normalized:
        normalized["fob_total_amount"] = header_total
    return normalized


def _read_pdf_dimensions(pdf_path: str) -> Tuple[int, int, int]:
    with pdfplumber.open(pdf_path) as pdf:
        page_count = len(pdf.pages)
        text_length = len(pdf.pages[0].extract_text() or "") if page_count else 0
    with fitz.open(pdf_path) as document:
        if not document.page_count:
            raise ValueError("PDF 文件没有页面")
        page = document.load_page(0)
        pixmap = page.get_pixmap(matrix=fitz.Matrix(0.25, 0.25), alpha=False)
        width, height = pixmap.width, pixmap.height
    logger.info("Customs PDF inspected: pages=%s first_page_text_chars=%s image=%sx%s", page_count, text_length, width, height)
    return page_count, width, height


def _extract_text_path(client: OpenAI, pdf_path: str) -> CustomsDeclarationData:
    with pdfplumber.open(pdf_path) as pdf:
        if not pdf.pages:
            raise ValueError("PDF 文件没有页面")
        first_page = pdf.pages[0]
        raw_text = first_page.extract_text() or ""
        tables = first_page.extract_tables() or []
    if len(raw_text) > 30000:
        logger.warning("PDF text truncated from %d to 30000 chars", len(raw_text))
        text = raw_text[:30000]
    else:
        text = raw_text
    system_prompt = (
        "你是中国海关进出口报关单结构化提取器；单据可能是《进口报关单》或《出口报关单》。根据文本与表格提取字段，只输出 JSON object。顶层字段必须包括 "
        "pre_entry_no, domestic_consignor, overseas_consignee, export_port, export_date, declaration_date, "
        "supervision_mode, transport_mode, package_count, package_type, gross_weight, net_weight, fob_total_amount, items。"
        "items 每项必须包括 item_no, hs_code, product_name, quantity, unit, unit_price, total_price, currency, "
        "origin_country, destination_country, duty_mode。金额、重量、件数、数量、单价必须为 JSON 数字。"
        "【进出口表头映射】进口日期映射到 export_date；进口单的进境关别映射到 export_port，严禁用‘入境口岸’代替进境关别；出口单使用出口日期和出境关别。"
        "境内收货人或境内发货人统一映射到 domestic_consignor；境外发货人或境外收货人统一映射到 overseas_consignee。"
        "【表体规则】若一行只有项号而没有商品编号(HS Code)和商品名称，该行是预印空白行，严禁输出到 items。"
        "若单价、总价、币制合并在同一格（例如‘单价 1000 / 总价 10000 / USD’），必须分别拆分为 unit_price=1000、total_price=10000、currency=USD。"
        "若数量及单位栏只有数字且没有单位文字，unit 必须为空字符串。"
        "无法识别的字符串填空字符串，无法识别的数值填 0；不得推测文档中不存在的数据。"
        "【严格类型约束】1. 所有日期(export_date, declaration_date)、编号(pre_entry_no, hs_code)、企业名称、贸易方式、单位、原产国、目的国及征免方式等文本字段必须输出为 JSON 字符串(String)；未找到时必须输出空字符串 \"\"，严禁输出数字 0 或 null！"
        "2. 若文本中日期为 8 位数字（如 20261026），请格式化为 \"YYYY-MM-DD\" 字符串。"
        "3. 所有重量、金额、数量、单价和件数字段必须输出为纯数字(Number)，不得包含单位或货币符号。"
    )
    user_prompt = f"页面文本：\n{text}\n\n页面表格：\n{json.dumps(tables, ensure_ascii=False)}"
    response = client.chat.completions.create(
        model=TEXT_EXTRACT_MODEL,
        messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
        response_format={"type": "json_object"},
        temperature=0,
        timeout=DEFAULT_LLM_TIMEOUT,
    )
    parsed = _normalize_and_clean_customs_data(_parse_json_content(response.choices[0].message.content))
    data = CustomsDeclarationData.model_validate(parsed)
    logger.info("[Text Path] Successfully extracted structured customs data via SiliconFlow")
    return data


def _is_vision_model_unavailable(exc: APIStatusError) -> bool:
    status_code = getattr(exc, "status_code", None)
    body = getattr(exc, "body", None)
    response = getattr(exc, "response", None)
    response_text = ""
    try:
        response_text = response.text if response is not None else ""
    except Exception:
        response_text = ""
    detail = f"{exc} {body!r} {response_text}".lower()
    return status_code in (400, 403, 404) or "30003" in detail or "model disabled" in detail


def _extract_vision_path(client: OpenAI, pdf_path: str) -> Dict[str, Any]:
    image_url = pdf_first_page_to_base64(pdf_path)
    messages = [{
        "role": "user",
        "content": [
            {"type": "text", "text": "图片可能是《进口报关单》或《出口报关单》。请核对并仅返回 JSON object：pre_entry_no、gross_weight、net_weight、package_count、fob_total_amount。进口单的日期和进境关别语义不得与入境口岸混淆。所有文本字段必须是 JSON 字符串，未识别填空字符串；重量、金额、件数必须为纯 JSON 数字。忽略只有项号、无 HS 编码和商品名称的预印空白商品行。"},
            {"type": "image_url", "image_url": {"url": image_url}},
        ],
    }]
    last_error: Optional[APIStatusError] = None
    for candidate_index, model_name in enumerate(VISION_MODEL_CANDIDATES):
        try:
            response = client.chat.completions.create(
                model=model_name,
                messages=messages,
                response_format={"type": "json_object"},
                temperature=0.0,
                max_tokens=2048,
                timeout=DEFAULT_LLM_TIMEOUT,
            )
            result = _parse_json_content(response.choices[0].message.content)
            logger.info("[Vision Path] Successfully extracted customs cross-check fields via %s", model_name)
            return result
        except APIStatusError as exc:
            last_error = exc
            if candidate_index + 1 >= len(VISION_MODEL_CANDIDATES) or not _is_vision_model_unavailable(exc):
                raise
            logger.warning("Vision model %s is unavailable; retrying with %s", model_name, VISION_MODEL_CANDIDATES[candidate_index + 1])
    if last_error:
        raise last_error
    raise RuntimeError("No vision extraction model candidates are configured")


def _demo_result(
    mode: str,
    *,
    source: str = "mock",
    reasons: Optional[List[str]] = None,
    vision_status: str = "skipped",
) -> CustomsExtractionResult:
    """Build a deterministic sandbox result that is always labelled as sample data."""
    if mode == "passed":
        data = MOCK_PASSED_DECLARATION.model_copy(deep=True)
        vision_data = dict(MOCK_PASSED_VISION)
    else:
        data = MOCK_DECLARATION.model_copy(deep=True)
        vision_data = dict(MOCK_ANOMALY_VISION)
    return CustomsExtractionResult(
        data=data,
        vision_data=vision_data,
        extraction_source=source,
        vision_status=vision_status,
        degraded_reasons=list(reasons or []),
    )


def _failure_message(text_error: Optional[str], vision_error: Optional[str]) -> str:
    details = []
    if text_error:
        details.append(f"文本路: {text_error[:160]}")
    if vision_error:
        details.append(f"视觉路: {vision_error[:160]}")
    suffix = f"（{'；'.join(details)}）" if details else ""
    return f"文本路与视觉路均提取失败，未产生真实提取结果{suffix}"


def extract_customs_dual_path(
    file_path: Optional[str], mode: str = "anomaly", *, allow_fallback: bool = False
) -> CustomsExtractionResult:
    """Extract declaration data with provenance; real mode fails closed unless fallback is allowed."""
    if mode in ("anomaly", "passed"):
        logger.info("Using customs sandbox sample (mode=%s)", mode)
        return _demo_result(mode, reasons=[f"demo_mode:{mode}"])
    if mode != "real":
        raise CustomsExtractionError(422, f"不支持的提取模式: {mode}", ["unsupported_mode"])

    if not file_path:
        raise CustomsExtractionError(422, "真实提取需要提供已上传的 PDF 文件", ["missing_pdf_file"])

    try:
        _read_pdf_dimensions(file_path)
    except Exception as exc:
        logger.exception("Could not inspect uploaded PDF for real customs extraction")
        if not allow_fallback:
            raise CustomsExtractionError(422, "PDF 文件无法读取或解析，未产生真实提取结果", ["pdf_unreadable"]) from exc
        return _demo_result(
            "anomaly",
            source="fallback",
            reasons=["pdf_unreadable", "fallback_requested"],
        )

    try:
        client = _client()
    except Exception as exc:
        logger.exception("Customs LLM client initialization failed")
        if not allow_fallback:
            raise CustomsExtractionError(
                503, "模型客户端初始化失败（未配置 API Key 或网络不可用）", ["client_init_failed"]
            ) from exc
        return _demo_result(
            "anomaly",
            source="fallback",
            reasons=["client_init_failed", "fallback_requested"],
        )

    text_error: Optional[str] = None
    vision_error: Optional[str] = None

    def _safe_text_extract() -> Optional[CustomsDeclarationData]:
        nonlocal text_error
        try:
            return _extract_text_path(client, file_path)
        except Exception as exc:
            text_error = str(exc)
            logger.warning("Text path extraction failed: %s", exc)
            return None

    def _safe_vision_extract() -> Optional[Dict[str, Any]]:
        nonlocal vision_error
        try:
            result = _extract_vision_path(client, file_path)
            return result or None
        except Exception as exc:
            vision_error = str(exc)
            logger.warning("Vision path extraction failed: %s", exc)
            return None

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            text_future = executor.submit(_safe_text_extract)
            vision_future = executor.submit(_safe_vision_extract)
            text_data = text_future.result()
            vision_data = vision_future.result()
    finally:
        client.close()

    if text_data and vision_data:
        normalized_text = CustomsDeclarationData.model_validate(
            _normalize_and_clean_customs_data(text_data.model_dump())
        )
        return CustomsExtractionResult(normalized_text, vision_data, "real", "ok", [])

    # A single-path result has no independent data to cross-check against, so the
    # vision payload stays empty: MODEL_CONFLICT must never compare a value with itself.
    if text_data:
        logger.warning("Customs dual-path degraded to text-only result")
        normalized_text = CustomsDeclarationData.model_validate(
            _normalize_and_clean_customs_data(text_data.model_dump())
        )
        return CustomsExtractionResult(
            normalized_text, {}, "real", "failed", ["vision_path_failed"]
        )

    if vision_data:
        logger.warning("Customs dual-path degraded to vision-only result")
        vision_declaration = CustomsDeclarationData.model_validate(_normalize_and_clean_customs_data({
            "pre_entry_no": vision_data.get("pre_entry_no", ""),
            "gross_weight": vision_data.get("gross_weight", 0),
            "net_weight": vision_data.get("net_weight", 0),
            "package_count": vision_data.get("package_count", 0),
            "fob_total_amount": vision_data.get("fob_total_amount", 0),
            "items": [],
        }))
        return CustomsExtractionResult(
            vision_declaration, {}, "real", "ok", ["text_path_failed"]
        )

    logger.error("Both paths in customs dual-path extraction failed")
    reasons = ["text_path_failed", "vision_path_failed"]
    if allow_fallback:
        return _demo_result(
            "anomaly",
            source="fallback",
            reasons=reasons + ["fallback_requested"],
            vision_status="failed",
        )
    raise CustomsExtractionError(503, _failure_message(text_error, vision_error), reasons)
