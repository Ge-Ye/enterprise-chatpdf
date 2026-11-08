import math
import re
from typing import List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator


_TEXT_EMPTY_VALUES = {"0", "null", "none"}
_NUMBER_PATTERN = re.compile(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)")


def _clean_text(value):
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, (int, float)) and value == 0:
        return ""
    text = str(value).strip()
    return "" if text.lower() in _TEXT_EMPTY_VALUES else text


def _clean_number(value, *, integer=False):
    if value is None or isinstance(value, bool) or (isinstance(value, str) and not value.strip()):
        return 0 if integer else 0.0
    try:
        if isinstance(value, str):
            match = _NUMBER_PATTERN.search(value.replace(",", "").strip())
            if match is None:
                return 0 if integer else 0.0
            value = match.group(0)
        number = float(value)
        if not math.isfinite(number):
            return 0 if integer else 0.0
        return int(number) if integer else number
    except (TypeError, ValueError, OverflowError):
        return 0 if integer else 0.0


class ExtractCustomsRequest(BaseModel):
    file_id: Optional[str] = None
    file_name: Optional[str] = None
    mode: Literal["real", "anomaly", "passed"] = "anomaly"
    # Real mode fails closed by default; sandbox samples require explicit consent.
    allow_fallback: bool = False


class FieldValidationIssue(BaseModel):
    field_path: str
    issue_type: Literal["MODEL_CONFLICT", "MATH_ERROR", "LOGIC_ERROR"]
    message: str
    ocr_value: Optional[Union[str, float, int]] = None
    vision_value: Optional[Union[str, float, int]] = None


class CustomsItem(BaseModel):
    item_no: int
    hs_code: str
    product_name: str
    quantity: float
    unit: str = ""
    unit_price: float
    total_price: float
    currency: str = ""
    origin_country: str = ""
    destination_country: str = ""
    duty_mode: str = ""

    @field_validator("hs_code", "product_name", "unit", "currency", "origin_country", "destination_country", "duty_mode", mode="before")
    @classmethod
    def normalize_text_fields(cls, value):
        return _clean_text(value)

    @field_validator("item_no", mode="before")
    @classmethod
    def normalize_item_number(cls, value):
        return _clean_number(value, integer=True)

    @field_validator("quantity", "unit_price", "total_price", mode="before")
    @classmethod
    def normalize_numeric_fields(cls, value):
        return _clean_number(value)


class CustomsDeclarationData(BaseModel):
    # Empty defaults keep missing model fields visible to validation; demo values
    # live only in MOCK_DECLARATION inside extractor_service.
    pre_entry_no: str = ""
    domestic_consignor: str = ""
    overseas_consignee: str = ""
    export_port: str = ""
    export_date: str = ""
    declaration_date: str = ""
    supervision_mode: str = ""
    transport_mode: str = ""
    package_count: int = 0
    package_type: str = ""
    gross_weight: float = 0.0
    net_weight: float = 0.0
    fob_total_amount: float = 0.0
    items: List[CustomsItem] = Field(default_factory=list)

    @field_validator(
        "pre_entry_no", "domestic_consignor", "overseas_consignee", "export_port",
        "export_date", "declaration_date", "supervision_mode", "transport_mode", "package_type",
        mode="before",
    )
    @classmethod
    def normalize_text_fields(cls, value):
        return _clean_text(value)

    @field_validator("package_count", mode="before")
    @classmethod
    def normalize_package_count(cls, value):
        return _clean_number(value, integer=True)

    @field_validator("gross_weight", "net_weight", "fob_total_amount", mode="before")
    @classmethod
    def normalize_numeric_fields(cls, value):
        return _clean_number(value)


class CustomsExtractionResponse(BaseModel):
    model_config = ConfigDict(use_enum_values=True)

    # DEMO marks sandbox/fallback samples; DEGRADED means a real result lost one path.
    status: Literal["PASSED", "NEEDS_REVIEW", "DEMO", "DEGRADED"]
    extraction_source: Literal["real", "mock", "fallback"]
    vision_status: Literal["ok", "failed", "skipped"]
    degraded_reasons: List[str] = Field(default_factory=list)
    feishu_alert_sent: bool
    feishu_alert_time: Optional[str] = None
    compliance_tip: Optional[str] = None
    data: CustomsDeclarationData
    issues: List[FieldValidationIssue] = Field(default_factory=list)
