"use client";

import { useMemo, useState } from "react";
import { API_BASE_URL } from "@/lib/api";

export interface FieldValidationIssue {
  field_path: string;
  issue_type: "MODEL_CONFLICT" | "MATH_ERROR" | "LOGIC_ERROR";
  message: string;
  ocr_value?: string | number;
  vision_value?: string | number;
}

export interface CustomsItem {
  item_no: number;
  hs_code: string;
  product_name: string;
  quantity: number;
  unit: string;
  unit_price: number;
  total_price: number;
  currency: string;
  origin_country: string;
  destination_country: string;
  duty_mode: string;
}

export interface CustomsExtractionResponse {
  status: "PASSED" | "NEEDS_REVIEW" | "DEMO" | "DEGRADED";
  extraction_source: "real" | "mock" | "fallback";
  vision_status: "ok" | "failed" | "skipped";
  degraded_reasons: string[];
  feishu_alert_sent: boolean;
  feishu_alert_time?: string;
  compliance_tip?: string;
  data: {
    pre_entry_no: string;
    domestic_consignor: string;
    overseas_consignee: string;
    export_port: string;
    export_date: string;
    declaration_date: string;
    supervision_mode: string;
    transport_mode: string;
    package_count: number;
    package_type: string;
    gross_weight: number;
    net_weight: number;
    fob_total_amount: number;
    items: CustomsItem[];
  };
  issues: FieldValidationIssue[];
}

export const MOCK_ANOMALY_DATA: CustomsExtractionResponse = {
  status: "DEMO",
  extraction_source: "mock",
  vision_status: "skipped",
  degraded_reasons: ["demo_mode:anomaly"],
  feishu_alert_sent: false,
  feishu_alert_time: undefined,
  compliance_tip: "【规则提示 · 涉证与归类】当前监管方式为『跨境电商B2B直接出口 (9710)』，最终目的国为『美国 (USA)』：① 项1 厨房水龙头(8481804090，单位：套) 出口美国须具备 NSF/ANSI 61 无铅认证及 DOE 水效注册。② 项2 不锈钢水槽(7324100000，原产国：中国 (CHN)) 须核查是否涉及美方 301 关税加征排除清单。",
  data: {
    pre_entry_no: "202651849302194857",
    domestic_consignor: "深圳市博润优品贸易有限公司 (4403169821)",
    overseas_consignee: "BORUN KITCHEN & BATH LLC (USA)",
    export_port: "深圳湾海关 (5345)",
    export_date: "2026-10-26",
    declaration_date: "2026-10-27",
    supervision_mode: "跨境电商B2B直接出口 (9710)",
    transport_mode: "水路运输 (2)",
    package_count: 70,
    package_type: "纸箱",
    gross_weight: 1180,
    net_weight: 1250,
    fob_total_amount: 15660,
    items: [
      { item_no: 1, hs_code: "8481804090", product_name: "304不锈钢厨房抽拉水龙头 (拉丝镍)", quantity: 500, unit: "套", unit_price: 18.5, total_price: 9250, currency: "USD", origin_country: "中国 (CHN)", destination_country: "美国 (USA)", duty_mode: "照章征税" },
      { item_no: 2, hs_code: "7324100000", product_name: "纳米黑金刚厨房手工水槽 (双槽)", quantity: 200, unit: "个", unit_price: 31.75, total_price: 6410, currency: "USD", origin_country: "中国 (CHN)", destination_country: "美国 (USA)", duty_mode: "照章征税" },
    ],
  },
  issues: [
    { field_path: "gross_weight", issue_type: "LOGIC_ERROR", message: "[表头逻辑拦截] 毛重(1180kg) 必须大于净重(1250kg)，疑似OCR行列错位（视觉路核对值：毛重1250kg / 净重1180kg）", ocr_value: 1180, vision_value: 1250 },
    { field_path: "items[1].total_price", issue_type: "MATH_ERROR", message: "[算术勾稽拦截] 数量(200个) × 单价(31.75) = 6350.00，与提取总价(6410.00)不符" },
    { field_path: "fob_total_amount", issue_type: "MODEL_CONFLICT", message: "[双模型比对冲突] pdfplumber文本路提取为 15660.00，Qwen-VL视觉路提取为 15600.00（印章遮挡干扰），请人工复核" },
  ],
};

export const MOCK_PASSED_DATA: CustomsExtractionResponse = {
  ...MOCK_ANOMALY_DATA,
  status: "DEMO",
  degraded_reasons: ["demo_mode:passed"],
  feishu_alert_sent: false,
  feishu_alert_time: undefined,
  data: { ...MOCK_ANOMALY_DATA.data, gross_weight: 1250, net_weight: 1180, fob_total_amount: 15600, items: MOCK_ANOMALY_DATA.data.items.map((item) => item.item_no === 2 ? { ...item, total_price: 6350 } : { ...item }) },
  issues: [],
};

type Mode = "anomaly" | "passed" | "api";
type EditableData = CustomsExtractionResponse["data"];

const inputClass = "w-full rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm text-gray-800 focus:outline-none focus:ring-2 focus:ring-blue-500";
const formatValue = (value: number) => Number.isInteger(value) ? String(value) : value.toFixed(2);
const DEGRADED_REASON_LABELS: Record<string, string> = {
  vision_path_failed: "视觉路不可用",
  text_path_failed: "文本路不可用",
  fallback_requested: "已启用回退样本",
  "demo_mode:anomaly": "演示模式：异常拦截",
  "demo_mode:passed": "演示模式：正常通过",
};
const reasonLabel = (reason: string) => DEGRADED_REASON_LABELS[reason] ?? reason;

export default function CustomsExtractorPanel({ fileName, fileId }: { fileName?: string | null; fileId?: string | null }) {
  const [mode, setMode] = useState<Mode>("anomaly");
  const [result, setResult] = useState<CustomsExtractionResponse>(MOCK_ANOMALY_DATA);
  const [form, setForm] = useState<EditableData>(MOCK_ANOMALY_DATA.data);
  const [loading, setLoading] = useState(false);
  const [message, setMessage] = useState("");
  const [showJson, setShowJson] = useState(false);
  const [hasRun, setHasRun] = useState(false);

  const getFieldIssue = (fieldPath: string) => result.issues.find((issue) => issue.field_path === fieldPath);
  const effectiveResult = useMemo(() => ({ ...result, data: form }), [result, form]);

  const updateHeader = <K extends keyof EditableData>(key: K, value: EditableData[K]) => setForm((current) => ({ ...current, [key]: value }));
  const updateItem = <K extends keyof CustomsItem>(index: number, key: K, value: CustomsItem[K]) => setForm((current) => ({ ...current, items: current.items.map((item, itemIndex) => itemIndex === index ? { ...item, [key]: value } : item) }));

  const runExtraction = async () => {
    setLoading(true);
    setMessage(mode === "api" ? "正在请求真实 API..." : "正在执行 pdfplumber 文本解析 + 视觉多模态比对 + 勾稽规则校验...");
    await new Promise((resolve) => window.setTimeout(resolve, 1200));
    if (mode === "api") {
      try {
        const response = await fetch(`${API_BASE_URL}/extract_customs`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ file_id: fileId ?? undefined, file_name: fileName ?? undefined, mode: "real" }) });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const payload = await response.json() as CustomsExtractionResponse;
        setResult(payload);
        setForm(payload.data);
        setHasRun(true);
        setMessage("真实 API 提取完成");
      } catch {
        setResult(MOCK_ANOMALY_DATA);
        setForm(MOCK_ANOMALY_DATA.data);
        setHasRun(true);
        setMessage("真实 API 不可用，已切换到演示数据（非真实结果）");
      }
    } else {
      const next = mode === "passed" ? MOCK_PASSED_DATA : MOCK_ANOMALY_DATA;
      setResult(next);
      setForm(next.data);
      setHasRun(true);
      setMessage("演示质检已完成");
    }
    setLoading(false);
  };

  const confirmReview = () => {
    const issues: FieldValidationIssue[] = [];
    form.items.forEach((item, index) => {
      if (Math.abs(item.quantity * item.unit_price - item.total_price) > 0.001) issues.push({ field_path: `items[${index}].total_price`, issue_type: "MATH_ERROR", message: `数量(${item.quantity}${item.unit}) × 单价(${item.unit_price.toFixed(2)}) = ${(item.quantity * item.unit_price).toFixed(2)}，与提取总价(${item.total_price.toFixed(2)})不符` });
      if (!item.unit.trim()) issues.push({ field_path: `items[${index}].unit`, issue_type: "LOGIC_ERROR", message: "成交计量单位不能为空" });
      if (!item.origin_country.trim()) issues.push({ field_path: `items[${index}].origin_country`, issue_type: "LOGIC_ERROR", message: "原产国不能为空" });
      if (!item.destination_country.trim()) issues.push({ field_path: `items[${index}].destination_country`, issue_type: "LOGIC_ERROR", message: "最终目的国不能为空" });
      if (!item.duty_mode.trim()) issues.push({ field_path: `items[${index}].duty_mode`, issue_type: "LOGIC_ERROR", message: "征免方式不能为空" });
    });
    if (form.gross_weight <= form.net_weight) issues.push({ field_path: "gross_weight", issue_type: "LOGIC_ERROR", message: `[表头逻辑拦截] 毛重(${form.gross_weight}kg) 必须大于净重(${form.net_weight}kg)，请复核重量数据` });
    if (!/^\d{18}$/.test(form.pre_entry_no)) issues.push({ field_path: "pre_entry_no", issue_type: "LOGIC_ERROR", message: "预录入编号应为 18 位数字" });
    const exportDate = Date.parse(form.export_date);
    const declarationDate = Date.parse(form.declaration_date);
    if (!form.declaration_date.trim() || Number.isNaN(exportDate) || Number.isNaN(declarationDate) || Math.abs(declarationDate - exportDate) > 90 * 24 * 60 * 60 * 1000) issues.push({ field_path: "declaration_date", issue_type: "LOGIC_ERROR", message: "申报日期或出口日期无效，或两者间隔超过 90 天" });
    if (!/(0110|9710|9810|9610|1210)/.test(form.supervision_mode)) issues.push({ field_path: "supervision_mode", issue_type: "LOGIC_ERROR", message: "监管方式为空或不属于常见贸易方式" });
    if (form.package_count <= 0) issues.push({ field_path: "package_count", issue_type: "LOGIC_ERROR", message: "包装件数必须大于 0" });
    const itemTotal = Math.round(form.items.reduce((sum, item) => sum + item.total_price, 0) * 100) / 100;
    if (Math.abs(form.fob_total_amount - itemTotal) > 0.01) issues.push({ field_path: "fob_total_amount", issue_type: "LOGIC_ERROR", message: `FOB 总金额(${form.fob_total_amount.toFixed(2)})与商品总价合计(${itemTotal.toFixed(2)})不一致` });
    const nextStatus: CustomsExtractionResponse["status"] = result.extraction_source !== "real"
      ? "DEMO"
      : issues.length
        ? "NEEDS_REVIEW"
        : result.degraded_reasons.length || result.vision_status !== "ok"
          ? "DEGRADED"
          : "PASSED";
    const nextResult: CustomsExtractionResponse = { ...result, status: nextStatus, feishu_alert_sent: issues.length ? result.feishu_alert_sent : false, data: form, issues };
    setResult(nextResult);
    setHasRun(true);
    setMessage(issues.length ? `复核完成，仍有 ${issues.length} 项待处理` : "人工复核通过，算术与逻辑校验全部通过");
  };

  const renderField = (label: string, path: string, value: string | number, onChange: (value: string) => void, type: "text" | "number" | "date" = "text") => {
    const issue = getFieldIssue(path);
    return <label key={path} className="block min-w-0 text-sm"><span className="mb-1 block font-medium text-gray-600">{label}</span><input type={type} step={type === "number" ? "any" : undefined} className={`${inputClass} ${issue ? "border-2 border-red-500 bg-red-50 text-red-900 focus:ring-red-500" : ""}`} value={value} onChange={(event) => onChange(event.target.value)} /><span aria-live="polite" className="mt-1 block text-xs text-red-600 font-medium">{issue?.message ?? ""}</span></label>;
  };

  const exportJson = () => {
    const blob = new Blob([JSON.stringify(effectiveResult, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = `${form.pre_entry_no || "customs-extraction"}.json`;
    anchor.click();
    URL.revokeObjectURL(url);
    setShowJson(true);
  };

  const bannerTone = result.status === "NEEDS_REVIEW" || result.status === "DEGRADED"
    ? "border-amber-300 bg-amber-50"
    : result.status === "DEMO"
      ? "border-slate-300 bg-slate-50"
      : "border-emerald-200 bg-emerald-50";
  const degradedMessage = result.vision_status === "failed"
    ? "仅文本路完成，视觉路不可用，需人工复核"
    : "仅视觉路完成，文本路不可用，需人工复核";
  const degradedNote = result.degraded_reasons.map(reasonLabel).join("、");

  return <section className="flex h-full min-h-0 flex-col overflow-y-auto rounded-b-xl p-5">
    <div className="flex flex-wrap items-center justify-between gap-3 border-b pb-4">
      <div className="min-w-0"><p className="text-xs font-medium uppercase tracking-wide text-gray-500">当前绑定单据</p><p className="truncate text-sm font-semibold text-gray-800">{fileName || "默认演示样本：深圳海关出口报关单_厨卫批次04.pdf"}</p></div>
      <label className="flex items-center gap-2 text-sm text-gray-600">模式<select className="rounded-lg border border-gray-300 bg-white px-3 py-2" value={mode} onChange={(event) => setMode(event.target.value as Mode)}><option value="anomaly">演示模式：异常拦截</option><option value="passed">演示模式：正常通过</option><option value="api">真实 API</option></select></label>
    </div>
    <div className="mt-4 flex flex-wrap items-center gap-3"><button type="button" disabled={loading} onClick={runExtraction} className="rounded-lg bg-blue-600 px-4 py-2.5 text-sm font-semibold text-white shadow-sm hover:bg-blue-700 disabled:cursor-wait disabled:opacity-60">⚡ 开始双路提取与质检</button>{loading && <span className="text-sm text-blue-700" aria-live="polite">{message}</span>}{!loading && message && <span className="text-sm text-gray-600" role="status">{message}</span>}</div>
    <div className={`mt-4 rounded-xl border p-4 ${bannerTone}`} role="status">
      {result.status === "DEMO" ? <><strong className="text-slate-800">🧪 演示/回退样本，非真实模型提取</strong><p className="mt-1 text-sm text-slate-600">数据仅供界面演示，请勿据此归档或报关{degradedNote ? `（${degradedNote}）` : ""}</p></> : result.status === "DEGRADED" ? <><strong className="text-amber-800">⚠️ 降级结果：{degradedMessage}</strong><p className="mt-1 text-sm text-amber-700">未完成双模型比对，规则校验 {result.issues.length} 项异常{degradedNote ? `（${degradedNote}）` : ""}</p></> : result.status === "NEEDS_REVIEW" ? <><div className="flex flex-wrap items-center justify-between gap-2"><strong className="text-red-800">🚨 质检异常，需人工复核</strong><span className="rounded-full bg-red-100 px-3 py-1 text-xs font-semibold text-red-800">规则校验 {result.issues.length} 项异常</span></div>{result.feishu_alert_sent && <div className="mt-2 inline-flex rounded-full bg-rose-100 px-3 py-1 text-xs font-semibold text-rose-800">🚨 已自动触发飞书 Webhook 告警卡片并 @关务复核组</div>}</> : <><strong className="text-emerald-800">✅ 双模型比对一致 &amp; 勾稽校验全部通过 - 已具备自动归档条件</strong><p className="mt-1 text-sm text-emerald-700">规则校验无异常项</p></>}
    </div>
    <div className="mt-5 w-full rounded-xl border border-gray-200 bg-white p-4 shadow-sm sm:p-5"><div className="mb-4 flex flex-wrap items-start justify-between gap-2"><h3 className="font-semibold text-gray-900">📄 报关单表头基本信息 (Header Information)</h3><div className="flex flex-wrap gap-2 text-xs"><span className="rounded-full bg-blue-50 px-2.5 py-1 font-medium text-blue-700">{form.supervision_mode}</span><span className="rounded-full bg-slate-100 px-2.5 py-1 text-slate-700">{form.export_port} ➔ {form.items[0]?.destination_country || "目的国未填"}</span></div></div><div className="grid grid-cols-1 gap-3 md:grid-cols-2 lg:grid-cols-3">
      {renderField("预录入/海关编号 (18位)", "pre_entry_no", form.pre_entry_no, (v) => updateHeader("pre_entry_no", v))}
      {renderField("境内发货人", "domestic_consignor", form.domestic_consignor, (v) => updateHeader("domestic_consignor", v))}
      {renderField("境外收货人", "overseas_consignee", form.overseas_consignee, (v) => updateHeader("overseas_consignee", v))}
      {renderField("出境关别", "export_port", form.export_port, (v) => updateHeader("export_port", v))}
      {renderField("出口日期", "export_date", form.export_date, (v) => updateHeader("export_date", v), "date")}
      {renderField("申报日期", "declaration_date", form.declaration_date, (v) => updateHeader("declaration_date", v), "date")}
      {renderField("监管方式", "supervision_mode", form.supervision_mode, (v) => updateHeader("supervision_mode", v))}
      {renderField("运输方式", "transport_mode", form.transport_mode, (v) => updateHeader("transport_mode", v))}
      {renderField("包装件数", "package_count", form.package_count, (v) => updateHeader("package_count", Number(v)), "number")}
      {renderField("包装种类", "package_type", form.package_type, (v) => updateHeader("package_type", v))}
      {renderField("毛重 (KG)", "gross_weight", formatValue(form.gross_weight), (v) => updateHeader("gross_weight", Number(v)), "number")}
      {renderField("净重 (KG)", "net_weight", formatValue(form.net_weight), (v) => updateHeader("net_weight", Number(v)), "number")}
      {renderField("FOB 申报总价 (USD)", "fob_total_amount", form.fob_total_amount.toFixed(2), (v) => updateHeader("fob_total_amount", Number(v)), "number")}
    </div></div>
    <div className="mt-5 w-full rounded-xl border border-gray-200 bg-white p-4 shadow-sm sm:p-5"><div className="mb-4"><h3 className="font-semibold text-gray-900">📦 报关单表体商品明细 (Commodity Items - 共 {form.items.length} 项)</h3><p className="mt-1 text-xs text-gray-500">商品归类、涉证字段与数量计价勾稽</p></div><div className="space-y-3">{form.items.map((item, index) => <div key={item.item_no} className="w-full rounded-lg border border-slate-200 bg-slate-50 p-3"><div className="mb-3 flex items-center justify-between"><h4 className="text-sm font-semibold text-gray-800">商品项 #{item.item_no}</h4><span className="text-xs text-gray-500">{item.currency}</span></div><div className="grid gap-x-3 gap-y-2 sm:grid-cols-2 xl:grid-cols-3">
        {renderField("项号", `items[${index}].item_no`, item.item_no, (v) => updateItem(index, "item_no", Number(v)), "number")}
        {renderField("HS 编码", `items[${index}].hs_code`, item.hs_code, (v) => updateItem(index, "hs_code", v))}
        {renderField("商品名称及规格", `items[${index}].product_name`, item.product_name, (v) => updateItem(index, "product_name", v))}
        {renderField("原产国", `items[${index}].origin_country`, item.origin_country, (v) => updateItem(index, "origin_country", v))}
        {renderField("最终目的国", `items[${index}].destination_country`, item.destination_country, (v) => updateItem(index, "destination_country", v))}
        {renderField("征免方式", `items[${index}].duty_mode`, item.duty_mode, (v) => updateItem(index, "duty_mode", v))}
        {renderField("数量", `items[${index}].quantity`, formatValue(item.quantity), (v) => updateItem(index, "quantity", Number(v)), "number")}
        {renderField("计量单位", `items[${index}].unit`, item.unit, (v) => updateItem(index, "unit", v))}
        {renderField("单价", `items[${index}].unit_price`, item.unit_price.toFixed(2), (v) => updateItem(index, "unit_price", Number(v)), "number")}
        {renderField("总价", `items[${index}].total_price`, item.total_price.toFixed(2), (v) => updateItem(index, "total_price", Number(v)), "number")}
        {renderField("币种", `items[${index}].currency`, item.currency, (v) => updateItem(index, "currency", v))}
      </div></div>)}</div></div>
    <div className="mt-4 flex flex-wrap gap-3"><button type="button" onClick={confirmReview} className="rounded-lg bg-emerald-600 px-4 py-2.5 text-sm font-semibold text-white hover:bg-emerald-700">✅ 人工复核确认并重新校验</button><button type="button" onClick={exportJson} className="rounded-lg border border-gray-300 bg-white px-4 py-2.5 text-sm font-semibold text-gray-700 hover:bg-gray-50">📥 导出标准 JSON / 入库报文</button></div>
    {showJson && <div className="mt-4 rounded-xl border border-gray-200 bg-gray-950 p-4"><div className="mb-2 flex justify-between text-sm font-semibold text-white"><span>最终 JSON 预览</span><button type="button" onClick={() => setShowJson(false)} className="text-gray-300 hover:text-white">收起</button></div><pre className="max-h-64 overflow-auto text-xs text-emerald-200">{JSON.stringify(effectiveResult, null, 2)}</pre></div>}
    {result.compliance_tip && <aside className="mt-5 rounded-xl border border-blue-200 bg-blue-50 p-4 text-sm text-blue-900"><p className="mb-1 font-semibold">🔎 规则提示：涉证与归类（本地规则，非知识库检索）</p><p>{result.compliance_tip}</p></aside>}
    {!hasRun && <p className="mt-4 text-xs text-gray-400">当前展示默认仿真数据；点击开始按钮以运行所选模式。</p>}
  </section>;
}
