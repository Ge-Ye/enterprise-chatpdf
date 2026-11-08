"""Deterministic customs validation, compliance hints, and Feishu alerts."""

import logging
from datetime import datetime
from typing import Any, Dict, List, Tuple

import requests

from app.core.config import settings
from app.schemas.customs_schema import CustomsDeclarationData, FieldValidationIssue

logger = logging.getLogger(__name__)


def validate_customs_declaration(
    data: CustomsDeclarationData, vision_data: Dict[str, Any]
) -> List[FieldValidationIssue]:
    issues: List[FieldValidationIssue] = []
    vision_fob = vision_data.get("fob_total_amount")
    if vision_fob is not None:
        try:
            vision_amount = float(vision_fob)
            if abs(data.fob_total_amount - vision_amount) > 0.01:
                issues.append(FieldValidationIssue(
                    field_path="fob_total_amount",
                    issue_type="MODEL_CONFLICT",
                    message=(f"[双模型比对冲突] pdfplumber+Qwen2.5-32B文本路提取为 {data.fob_total_amount:.2f}，"
                             f"PyMuPDF+Qwen3-VL视觉路提取为 {vision_amount:.2f}（印章遮挡干扰），请人工复核"),
                    ocr_value=data.fob_total_amount,
                    vision_value=vision_amount,
                ))
        except (TypeError, ValueError):
            issues.append(FieldValidationIssue(field_path="fob_total_amount", issue_type="LOGIC_ERROR", message="视觉路返回的 FOB 总金额不是有效数字"))

    for index, item in enumerate(data.items):
        expected = round(item.quantity * item.unit_price, 2)
        if abs(expected - item.total_price) > 0.001:
            issues.append(FieldValidationIssue(
                field_path=f"items[{index}].total_price",
                issue_type="MATH_ERROR",
                message=f"[算术勾稽拦截] 数量({item.quantity:g}{item.unit}) × 单价({item.unit_price:.2f}) = {expected:.2f}，与提取总价({item.total_price:.2f})不符",
                ocr_value=item.total_price,
            ))
        if not item.unit.strip():
            issues.append(FieldValidationIssue(field_path=f"items[{index}].unit", issue_type="LOGIC_ERROR", message="成交计量单位不能为空"))
        if not item.origin_country.strip():
            issues.append(FieldValidationIssue(field_path=f"items[{index}].origin_country", issue_type="LOGIC_ERROR", message="原产国不能为空"))
        if not item.destination_country.strip():
            issues.append(FieldValidationIssue(field_path=f"items[{index}].destination_country", issue_type="LOGIC_ERROR", message="最终目的国不能为空"))
        if not item.duty_mode.strip():
            issues.append(FieldValidationIssue(field_path=f"items[{index}].duty_mode", issue_type="LOGIC_ERROR", message="征免方式不能为空"))

    if data.gross_weight <= data.net_weight:
        vision_gross = vision_data.get("gross_weight", "未提供")
        vision_net = vision_data.get("net_weight", "未提供")
        issues.append(FieldValidationIssue(
            field_path="gross_weight",
            issue_type="LOGIC_ERROR",
            message=(f"[表头逻辑拦截] 毛重({data.gross_weight:g}kg) 必须大于净重({data.net_weight:g}kg)，"
                     f"疑似OCR行列错位（视觉路核对值：毛重{vision_gross}kg / 净重{vision_net}kg）"),
            ocr_value=data.gross_weight,
            vision_value=vision_gross if isinstance(vision_gross, (str, int, float)) else None,
        ))
    if len(data.pre_entry_no) != 18 or not data.pre_entry_no.isdigit():
        issues.append(FieldValidationIssue(field_path="pre_entry_no", issue_type="LOGIC_ERROR", message="预录入编号应为 18 位数字"))

    try:
        export_date = datetime.strptime(data.export_date.strip(), "%Y-%m-%d").date()
        declaration_date = datetime.strptime(data.declaration_date.strip(), "%Y-%m-%d").date()
        if abs((declaration_date - export_date).days) > 90:
            raise ValueError("申报日期与出口日期间隔超过 90 天")
    except (ValueError, AttributeError):
        issues.append(FieldValidationIssue(
            field_path="declaration_date",
            issue_type="LOGIC_ERROR",
            message="[日期逻辑拦截] 出口日期或申报日期格式无效，或两者间隔超过 90 天",
        ))

    common_modes = ("0110", "9710", "9810", "9610", "1210")
    if not data.supervision_mode.strip() or not any(code in data.supervision_mode for code in common_modes):
        issues.append(FieldValidationIssue(field_path="supervision_mode", issue_type="LOGIC_ERROR", message="监管方式为空或不属于常见贸易方式（0110/9710/9810/9610/1210）"))
    if data.package_count <= 0:
        issues.append(FieldValidationIssue(field_path="package_count", issue_type="LOGIC_ERROR", message="包装件数必须大于 0"))

    item_total = round(sum(item.total_price for item in data.items), 2)
    if abs(data.fob_total_amount - item_total) > 0.01:
        issues.append(FieldValidationIssue(field_path="fob_total_amount", issue_type="LOGIC_ERROR", message=f"FOB 总金额({data.fob_total_amount:.2f})与商品总价合计({item_total:.2f})不一致"))
    return issues


def send_feishu_webhook_alert(
    data: CustomsDeclarationData, issues: List[FieldValidationIssue]
) -> Tuple[bool, str]:
    webhook_url = settings.FEISHU_WEBHOOK_URL.strip()
    if not webhook_url:
        logger.warning("FEISHU_WEBHOOK_URL is not configured; skipping customs alert")
        return False, ""

    issue_text = "\n".join(f"• {issue.message}" for issue in issues)
    payload = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": "red",
                "title": {"tag": "plain_text", "content": "🚨 【报关单AI质检告警】发现数据异常待人工复核"},
            },
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": f"**单据号：** {data.pre_entry_no}\n**提取模型：** Qwen2.5-32B vs Qwen3-VL\n**异常数量：** {len(issues)}\n\n{issue_text}"}},
            ],
        },
    }
    try:
        response = requests.post(webhook_url, json=payload, timeout=5)
        response.raise_for_status()
        try:
            body = response.json()
        except ValueError:
            body = None
        if isinstance(body, dict) and body.get("code", 0) not in (0, None):
            logger.error("Feishu rejected customs alert: code=%s msg=%s", body.get("code"), body.get("msg", ""))
            return False, ""
        result_time = datetime.now().astimezone().isoformat(timespec="seconds")
        logger.info("Feishu customs alert delivered: HTTP %s; response=%s", response.status_code, response.text[:500])
        return True, result_time
    except requests.RequestException:
        # Requests exceptions include the webhook URL, which contains the bot token.
        logger.warning("Failed to deliver Feishu customs alert (request error: %s)", "network or HTTP transport failure")
        return False, ""


def build_compliance_rule_tips(data: CustomsDeclarationData) -> str:
    """Hard-coded HS-code rule hints; this is not a knowledge-base retrieval."""
    destinations = ", ".join(dict.fromkeys(item.destination_country for item in data.items if item.destination_country.strip())) or "未填写"
    tips = [f"【规则提示 · 涉证与归类】当前监管方式为『{data.supervision_mode}』，最终目的国为『{destinations}』："]
    for item in data.items:
        destination = item.destination_country.upper()
        if item.hs_code == "8481804090" or "水龙头" in item.product_name:
            if "USA" in destination or "美国" in item.destination_country:
                tips.append(f"① 项{item.item_no} 厨房水龙头({item.hs_code}，单位：{item.unit}) 出口美国须具备 NSF/ANSI 61 无铅认证及 DOE 水效注册。")
            else:
                tips.append(f"项{item.item_no} 厨房水龙头({item.hs_code}，单位：{item.unit})：按最终目的国核验涉证要求。")
        if item.hs_code == "7324100000" or "水槽" in item.product_name:
            tips.append(f"② 项{item.item_no} 不锈钢水槽({item.hs_code}，原产国：{item.origin_country}) 须核查是否涉及美方 301 关税加征排除清单。")
    return " ".join(tips) if len(tips) > 1 else ""
